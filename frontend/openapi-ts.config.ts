import { defineConfig } from '@hey-api/openapi-ts';

// frontend/openapi-ts.config.ts
//
// Source of truth for the TS client generation step (scripts/gen_client.sh).
// Input:  frontend/openapi.json  (committed; refreshed by scripts/gen_schema.sh)
// Output: frontend/src/api/generated/  (committed; diff-gated in CI)
//
// Plugins are kept minimal:
//   @hey-api/typescript  — request/response types per operation
//   @hey-api/sdk         — typed SDK functions (one per operationId)
//
// We deliberately omit prettier post-processing: adding prettier as a
// frontend devDep just to format generated code is more weight than it
// is worth. The generator's default output is already deterministic and
// stable across versions, which is what the CI stale-gate needs.
export default defineConfig({
  input: './openapi.json',
  output: {
    path: 'src/api/generated',
    // Empty postProcess => no formatter / linter pipeline. The CLI still
    // surfaces a `lint is deprecated` notice on stderr when format/lint
    // keys are present; leaving them out is the supported spelling now.
  },
  services: {
    // No custom `asClass`; we emit flat SDK functions (matches HEY v0.99
    // default and keeps the public surface small).
  },
});
