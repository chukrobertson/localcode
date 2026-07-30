# Third-Party Software

## MemPalace

- Project: https://github.com/MemPalace/mempalace
- Vendored revision: `aa89bd82272f55381206c83b6f306e79351824eb`
- License: MIT, retained in `vendor/mempalace/LICENSE`
- Integration: installed into an isolated virtual environment or package directory and invoked as a local subprocess

MemPalace is not imported into the GTK process. This keeps its vector database and numerical
dependencies isolated from LocalCode and makes memory support independently replaceable.
