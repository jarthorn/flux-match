# Objective

You are a coding assistant for a developer. Read README.md for a description of this project.

# Tech stack

- uv for environment management
- Python 3.15
- Ruff for python formatting. Please run ruff check and ruff format on generated code.

# Conventions

- Raw input data should be stored in the "data" folder, in a sub-folder appropriate for that script
- Output files should be stored in the "output" folder. Output filenames should have a scenario or script name, and a timestamp in the format YYYYMMDD-HHMM from when the output was produced
- Batch scripts go in "bin" folder
- Follow the coding conventions of an experienced programmer:
  - Minimize code duplication. Use helper functions within a script to avoid duplication, and propose refactorings where duplication is emerging across scripts.
  - Write test cases for complex logic, and regression tests when fixing bugs. I am not concerned with complete code coverage, but focus testing effort on areas with the potential for subtle bugs.
  - Malformed or missing input data should not be ignored. Flag data errors to stderr so that we can follow up and validate input data quality.
  - It is better for a script to fail than produce an incorrect output. Hard fail and exit when an important assumption is invalidated by the code path.