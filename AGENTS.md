# Codex Review Rules

## Project Context
- This is a production system. Stability matters more than speed.
- Focus on real bugs, regressions, security risks, and maintainability.

## Architecture Rules
- No direct database access from the frontend.
- Business logic belongs in backend services.
- Avoid tight coupling across modules.
- Prefer async workflows over blocking calls where appropriate.

## Review Priorities
1. Correctness
2. Security
3. Reliability
4. Performance
5. Maintainability

## Treat as critical
- Data corruption risk
- Race conditions
- Broken API contracts
- Missing auth or unsafe secret handling

## Ignore
- Cosmetic formatting
- Minor refactors that do not affect readability or safety
