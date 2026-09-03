sed -i '78,95c\
## Installation\
\
Lattice installs as a Python package, or compiles into a standalone binary (PyInstaller, `hatch run build-bin`).\
\
**Option 1: pipx (recommended)**\
```bash\
pipx install lattice-music\
# now you can run `lattice` globally\
```\
\
**Option 2: pip (virtual environment)**\
```bash\
pip install lattice-music\
```\
\
## Requirements\
' ~/.gitrepos/Lattice/README.md
