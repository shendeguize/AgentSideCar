"""Python 3.8 compatibility gates for remote observation payloads.

``ast.parse`` checks syntax only; it does not validate standard-library API
availability.  The companion AST scan below conservatively covers the known
Python 3.9+ syntax/API hazards identified by the approved compatibility
research.  It is intentionally documented as known-feature coverage, not as
an exhaustive Python 3.8 API compatibility proof.
"""

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_ROOT = REPO_ROOT / "sidecar"
PYTHON_38_FEATURE_VERSION = (3, 8)
EXPECTED_REMOTE_PAYLOADS = frozenset(
    (
        ("sidecar/remote_transport.py", "_PROBE_CODE"),
        ("sidecar/remote_transport.py", "_ROOT_MAIN"),
        ("sidecar/remote_transport.py", "REMOTE_BOOTSTRAP"),
        ("sidecar/remote_watch_transport.py", "REMOTE_WATCH_BOOTSTRAP"),
    )
)

_RUNTIME_GENERIC_BUILTINS = frozenset(
    ("dict", "frozenset", "list", "set", "tuple", "type")
)
_PYTHON_39_MODULES = frozenset(("graphlib", "zoneinfo"))
_PYTHON_39_FROM_IMPORTS = frozenset(
    (
        ("ast", "unparse"),
        ("functools", "cache"),
        ("os", "pidfd_open"),
        ("random", "randbytes"),
        ("socket", "recv_fds"),
        ("socket", "send_fds"),
        ("typing", "Annotated"),
    )
)
_PYTHON_39_CALLS = frozenset(
    "{}.{}".format(module, name) for module, name in _PYTHON_39_FROM_IMPORTS
)
_PYTHON_39_METHODS = frozenset(
    (
        "is_relative_to",
        "randbytes",
        "readlink",
        "recv_fds",
        "removeprefix",
        "removesuffix",
        "send_fds",
        "with_stem",
    )
)
_PYTHON_39_POPEN_KEYWORDS = frozenset(
    ("extra_groups", "group", "umask", "user")
)


def _relative(path):
    return path.relative_to(REPO_ROOT).as_posix()


def _parse(source, filename):
    return ast.parse(
        source,
        filename=filename,
        feature_version=PYTHON_38_FEATURE_VERSION,
    )


def _static_python_source(expression):
    if isinstance(expression, ast.Constant) and isinstance(
        expression.value,
        (bytes, str),
    ):
        value = expression.value
        return value.decode("utf-8") if isinstance(value, bytes) else value
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        left = _static_python_source(expression.left)
        right = _static_python_source(expression.right)
        return None if left is None or right is None else left + right
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and not expression.keywords
    ):
        value = _static_python_source(expression.func.value)
        if value is None:
            return None
        if expression.func.attr == "strip" and not expression.args:
            return value.strip()
        if expression.func.attr == "encode" and len(expression.args) <= 1:
            if not expression.args:
                return value
            encoding = expression.args[0]
            if (
                isinstance(encoding, ast.Constant)
                and encoding.value in ("utf-8", "UTF-8")
            ):
                return value
    return None


def _module_payload_assignments(tree):
    assignments = {}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else (statement.target,)
        )
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        value = _static_python_source(statement.value)
        if value is not None:
            assignments[targets[0].id] = value
    return assignments


def _payload_reference_expressions(tree):
    references = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            for index, element in enumerate(node.elts[:-1]):
                if isinstance(element, ast.Constant) and element.value == "-c":
                    references.append(node.elts[index + 1])
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "__main__.py"
                ):
                    references.append(value)
    return references


def _discover_python_payloads(source_units):
    payloads = {}
    discovered_names = set()
    unsupported = []
    for path, source, tree in source_units:
        assignments = _module_payload_assignments(tree)
        for expression in _payload_reference_expressions(tree):
            if isinstance(expression, ast.Name):
                name = expression.id
                discovered_names.add((_relative(path), name))
                payload = assignments.get(name)
                if payload is None:
                    unsupported.append(
                        "{}:{} ({})".format(
                            _relative(path),
                            getattr(expression, "lineno", "?"),
                            name,
                        )
                    )
                else:
                    payloads["{}:{}".format(_relative(path), name)] = payload
            else:
                payload = _static_python_source(expression)
                if payload is None:
                    unsupported.append(
                        "{}:{} ({})".format(
                            _relative(path),
                            getattr(expression, "lineno", "?"),
                            type(expression).__name__,
                        )
                    )
                else:
                    payloads[
                        "{}:{}:<literal>".format(
                            _relative(path),
                            getattr(expression, "lineno", "?"),
                        )
                    ] = payload
    return payloads, discovered_names, unsupported


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is not None:
            return "{}.{}".format(parent, node.attr)
    return None


class _KnownPython39HazardScanner(ast.NodeVisitor):
    """Conservative AST coverage for the compatibility research hazard list."""

    def __init__(self, tree):
        self.aliases = {}
        self.annotation_nodes = set()
        self.dict_names = set()
        self.hazards = []
        self._collect_aliases(tree)
        self._collect_annotation_nodes(tree)
        self._collect_dict_names(tree)

    def _collect_aliases(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    self.aliases[bound] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if alias.name != "*":
                        bound = alias.asname or alias.name
                        self.aliases[bound] = "{}.{}".format(
                            node.module,
                            alias.name,
                        )

    def _collect_annotation_nodes(self, tree):
        annotations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.arg) and node.annotation is not None:
                annotations.append(node.annotation)
            elif isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)
            elif isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ) and node.returns is not None:
                annotations.append(node.returns)
        for annotation in annotations:
            self.annotation_nodes.update(id(node) for node in ast.walk(annotation))

    def _collect_dict_names(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else (node.target,)
                )
                if self._dict_like_expression(value):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            self.dict_names.add(target.id)
                if isinstance(node, ast.AnnAssign):
                    annotation = self._resolved_name(node.annotation)
                    if annotation in ("dict", "typing.Dict"):
                        if isinstance(node.target, ast.Name):
                            self.dict_names.add(node.target.id)
            elif isinstance(node, ast.arg) and node.annotation is not None:
                if self._resolved_name(node.annotation) in ("dict", "typing.Dict"):
                    self.dict_names.add(node.arg)

    def _resolved_name(self, node):
        dotted = _dotted_name(node)
        if dotted is None:
            return None
        head, separator, tail = dotted.partition(".")
        resolved = self.aliases.get(head, head)
        return resolved + (separator + tail if separator else "")

    def _dict_like_expression(self, node):
        return isinstance(node, ast.Dict) or (
            isinstance(node, ast.Call)
            and self._resolved_name(node.func) in ("dict", "builtins.dict")
        )

    def _possible_dict_value(self, node):
        return self._dict_like_expression(node) or (
            isinstance(node, ast.Name) and node.id in self.dict_names
        )

    def _report(self, node, message):
        finding = (getattr(node, "lineno", 0), message)
        if finding not in self.hazards:
            self.hazards.append(finding)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name.split(".")[0] in _PYTHON_39_MODULES:
                self._report(node, "Python 3.9 stdlib module {}".format(alias.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        if module.split(".")[0] in _PYTHON_39_MODULES:
            self._report(node, "Python 3.9 stdlib module {}".format(module))
        for alias in node.names:
            if (module, alias.name) in _PYTHON_39_FROM_IMPORTS:
                self._report(
                    node,
                    "Python 3.9 stdlib API {}.{}".format(module, alias.name),
                )
        self.generic_visit(node)

    def visit_Subscript(self, node):
        resolved = self._resolved_name(node.value)
        if resolved == "typing.Annotated":
            self._report(node, "typing.Annotated requires Python 3.9")
        if (
            id(node) not in self.annotation_nodes
            and isinstance(node.value, ast.Name)
            and node.value.id in _RUNTIME_GENERIC_BUILTINS
        ):
            self._report(
                node,
                "runtime built-in generic {}[...] requires Python 3.9".format(
                    node.value.id
                ),
            )
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.BitOr):
            if id(node) in self.annotation_nodes:
                self._report(node, "PEP 604 annotation union requires Python 3.10")
            elif self._possible_dict_value(node.left) or self._possible_dict_value(
                node.right
            ):
                self._report(node, "dictionary union requires Python 3.9")
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        if (
            isinstance(node.op, ast.BitOr)
            and isinstance(node.target, ast.Name)
            and node.target.id in self.dict_names
        ):
            self._report(node, "dictionary union assignment requires Python 3.9")
        self.generic_visit(node)

    def visit_Call(self, node):
        resolved = self._resolved_name(node.func)
        if resolved in _PYTHON_39_CALLS:
            self._report(node, "Python 3.9 stdlib API {}".format(resolved))
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _PYTHON_39_METHODS
            and not (node.func.attr == "readlink" and resolved == "os.readlink")
        ):
            self._report(
                node,
                "known Python 3.9 method {}".format(node.func.attr),
            )
        if resolved == "subprocess.Popen":
            keywords = {keyword.arg for keyword in node.keywords}
            unsupported = sorted(keywords & _PYTHON_39_POPEN_KEYWORDS)
            if unsupported:
                self._report(
                    node,
                    "Python 3.9 subprocess.Popen keyword(s): {}".format(
                        ", ".join(unsupported)
                    ),
                )
        self.generic_visit(node)

    def _visit_function_decorators(self, node):
        for decorator in node.decorator_list:
            if self._resolved_name(decorator) == "functools.cache":
                self._report(decorator, "functools.cache requires Python 3.9")

    def visit_FunctionDef(self, node):
        self._visit_function_decorators(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function_decorators(node)
        self.generic_visit(node)


def _known_hazards(tree):
    scanner = _KnownPython39HazardScanner(tree)
    scanner.visit(tree)
    return sorted(scanner.hazards)


class RemotePayloadCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_units = []
        for path in sorted(SIDECAR_ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            cls.source_units.append((path, source, _parse(source, _relative(path))))
        (
            cls.payloads,
            cls.discovered_payload_names,
            cls.unsupported_payloads,
        ) = _discover_python_payloads(cls.source_units)

    def test_all_sidecar_sources_parse_as_python_38(self):
        self.assertTrue(self.source_units)
        for path, source, _tree in self.source_units:
            with self.subTest(path=_relative(path)):
                _parse(source, _relative(path))

    def test_payload_discovery_is_complete_and_payloads_parse_as_python_38(self):
        self.assertEqual([], self.unsupported_payloads)
        self.assertTrue(
            EXPECTED_REMOTE_PAYLOADS.issubset(self.discovered_payload_names),
            "missing expected remote payload(s): {}".format(
                sorted(EXPECTED_REMOTE_PAYLOADS - self.discovered_payload_names)
            ),
        )
        self.assertTrue(self.payloads)
        for label, payload in sorted(self.payloads.items()):
            with self.subTest(payload=label):
                _parse(payload, label)

    def test_sidecar_sources_avoid_known_python_39_plus_hazards(self):
        findings = []
        for path, _source, tree in self.source_units:
            findings.extend(
                "{}:{}: {}".format(_relative(path), line, message)
                for line, message in _known_hazards(tree)
            )
        self.assertEqual(
            [],
            findings,
            "conservative known-feature compatibility findings:\n{}".format(
                "\n".join(findings)
            ),
        )

    def test_payloads_avoid_known_python_39_plus_hazards(self):
        findings = []
        for label, payload in sorted(self.payloads.items()):
            tree = _parse(payload, label)
            findings.extend(
                "{}:{}: {}".format(label, line, message)
                for line, message in _known_hazards(tree)
            )
        self.assertEqual(
            [],
            findings,
            "conservative known-feature payload findings:\n{}".format(
                "\n".join(findings)
            ),
        )


if __name__ == "__main__":
    unittest.main()
