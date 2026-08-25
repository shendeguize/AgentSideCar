import { Input } from '@deepseek-ai/dsh-client-ui-primitives'
import { useEffect, useId, useState } from 'react'
import type { ReactElement } from 'react'
import css from './settings-card.module.css'

interface FieldCopy {
  label: string
  hint: string
  disabled: boolean
}

interface SelectFieldProps extends FieldCopy {
  value: string
  options: readonly { value: string, label: string }[]
  onCommit: (value: string) => void
}

export function SelectField(props: SelectFieldProps): ReactElement {
  const id = useId()
  return (
    <div className={css['field']}>
      <label className={css['label']} htmlFor={id}>{props.label}</label>
      <select
        id={id}
        className={css['select']}
        value={props.value}
        disabled={props.disabled}
        onChange={(event) => { props.onCommit(event.target.value) }}
      >
        {props.options.map(option => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
      <p className={css['hint']}>{props.hint}</p>
    </div>
  )
}

interface ToggleFieldProps extends FieldCopy {
  checked: boolean
  onCommit: (checked: boolean) => void
}

export function ToggleField(props: ToggleFieldProps): ReactElement {
  return (
    <div className={css['field']}>
      <label className={css['toggleRow']}>
        <input
          type="checkbox"
          checked={props.checked}
          disabled={props.disabled}
          onChange={(event) => { props.onCommit(event.target.checked) }}
        />
        <span className={css['label']}>{props.label}</span>
      </label>
      <p className={css['hint']}>{props.hint}</p>
    </div>
  )
}

interface TextFieldProps extends FieldCopy {
  value: string
  placeholder?: string
  onCommit: (value: string) => void
}

export function TextField(props: TextFieldProps): ReactElement {
  const id = useId()
  const [draft, setDraft] = useState(props.value)
  useEffect(() => { setDraft(props.value) }, [props.value])
  return (
    <div className={css['field']}>
      <label className={css['label']} htmlFor={id}>{props.label}</label>
      <Input
        id={id}
        className={css['input']}
        type="text"
        value={draft}
        placeholder={props.placeholder ?? ''}
        disabled={props.disabled}
        onChange={(event) => {
          setDraft(event.target.value)
          props.onCommit(event.target.value)
        }}
      />
      <p className={css['hint']}>{props.hint}</p>
    </div>
  )
}

interface NumberFieldProps extends FieldCopy {
  invalidHint: string
  min: number
  value: number
  onCommit: (value: number) => void
}

export function NumberField(props: NumberFieldProps): ReactElement {
  const id = useId()
  const [draft, setDraft] = useState(String(props.value))
  const [invalid, setInvalid] = useState(false)
  useEffect(() => {
    setDraft(String(props.value))
    setInvalid(false)
  }, [props.value])
  return (
    <div className={css['field']}>
      <label className={css['label']} htmlFor={id}>{props.label}</label>
      <Input
        id={id}
        className={`${css['input']} ${invalid ? css['inputInvalid'] : ''}`}
        type="text"
        inputMode="numeric"
        value={draft}
        disabled={props.disabled}
        {...invalid ? { 'aria-invalid': true } : {}}
        onChange={(event) => {
          const text = event.target.value
          const parsed = Number(text)
          const acceptable = text.trim() !== '' && Number.isInteger(parsed) && parsed >= props.min
          setDraft(text)
          setInvalid(!acceptable)
          if (acceptable && parsed !== props.value) props.onCommit(parsed)
        }}
      />
      <p className={invalid ? css['invalidHint'] : css['hint']} {...invalid ? { role: 'alert' } : {}}>
        {invalid ? props.invalidHint : props.hint}
      </p>
    </div>
  )
}
