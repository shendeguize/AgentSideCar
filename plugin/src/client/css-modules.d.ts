/**
 * CSS Modules ambient declaration for the client TS program (same shape as
 * the dsh-web-ui blueprint packages). The build side is handled by the
 * `dsh-css-modules-inline` plugin in tsdown.client.ts, which compiles
 * `*.module.css` with lightningcss into a hashed class map and a
 * self-injecting `<style data-plugin>` tag.
 */
declare module '*.module.css' {
  const classes: Record<string, string>
  export default classes
}
