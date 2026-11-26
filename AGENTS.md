# Repository workflow

- Run `make coverage` after code changes. It runs the test suite and requires at least 85% line coverage across the Python application, CLI, and container code.
- Fix test failures and close coverage gaps before committing and pushing the task changes to the configured GitHub branch.
- For changes to Docker execution or agent coordination, also run `make smoke` against the local images.
