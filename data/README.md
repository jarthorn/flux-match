# data

Raw input data, one sub-folder per source. Everything here except this file is ignored by git.

- `coders/`: files downloaded from [CODERS](https://coders.cme-emh.ca/) by `flux-match fetch`.
  Existing files are not re-downloaded; delete them to force a refresh. Hourly tables always
  have 8760 hours: February 29 is dropped from leap years, and later hours are renumbered.
