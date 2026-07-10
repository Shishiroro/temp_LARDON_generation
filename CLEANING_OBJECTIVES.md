Little readme to keep track of what to do to clean the repo

- Remove all that is related to evaluation, YOLO or stuff like that
- Refactor the sources (Export.py up one level) [OK]
- Make imports to LARD clean (no direct use of modules, find a way to import as library)
- Make imports to TAF clean (same thing)
- Create project.toml for UV installation (@mathieu)
- Remove _paths.py usage:
  - for modules inside LARDON (source code): either use path from LARDON root, or use relative
  - for external modules (LARD, TAF): get path to them, create install script (using pip or other) to install local packages.
- Clean run_pipeline.py (rename as main.py) + remove any mention of Evaluation

Also, sometimes there are imports inside functions. It is not very recommended (except when needed). I removed most of them and put them at file top. For some (e.g., Taf), I leave it like this, I suppose it was done on purpose. TODO : verify that all the import we can put at files top are effectively there !

From what I understand:

- Export.py
- Generate.py

are for only TAF work, i.e., they work for scenario generation

Image creation is in runs.py
