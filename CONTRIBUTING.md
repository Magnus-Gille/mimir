# Contributing

Issues and pull requests are welcome. Keep changes focused and avoid including
real infrastructure addresses, account identifiers, credentials, customer data,
or production status in examples, fixtures, logs, screenshots, and commit messages.

## Development

```bash
npm ci
npm run lint
npx tsc --noEmit
npm test
npm run build
bash -n scripts/*.sh
shellcheck --severity=warning scripts/*.sh
```

For non-trivial behavior changes, add a regression test that fails for the intended
reason before changing the implementation. Security-sensitive changes should state
the trust boundary and expected failure mode in the pull request.

By contributing, you agree that your contribution is licensed under the MIT License.
