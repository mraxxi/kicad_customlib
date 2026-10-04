# KiCad Custom Library (`KICAD_CUSTOM_LIB`)

A centralized, portable custom component library and automation suite for KiCad 10+.

---

## Features
- **Single-Import Architecture**: Includes all custom symbols, footprints, and 3D models into any project via a single master table reference.
- **Cross-Platform Sync**: Zero hardcoded absolute paths — everything uses the standardized `${KICAD_CUSTOM_LIB}` environment variable.
- **Intelligent Staging & Ingestion**: Drop raw part downloads (ZIP or folders) into `staging-temp/` or use the CLI/GUI to automatically route files to `symbols/`, `footprints/`, and `3dmodels/`.
- **Database & History Tracking (`manifest.json`)**: Keeps a permanent record of original component names, sources, import dates, and rename/reorganize history.
- **Lean Project Packager**: Export *only* the specific components used in a given project into a self-contained local folder for clean GitHub distribution without library bloat.
- **Dual CLI & GUI**: Manage the library with either an interactive desktop app (`python scripts/lib_manager.py`) or headless script commands.

---

## Repository Layout

```
KICAD_CUSTOM_LIB/
├── sym-lib-table               # Master symbol library table
├── fp-lib-table                # Master footprint library table
├── manifest.json               # Database of components, metadata, and history
├── symbols/                    # Symbol libraries (*.kicad_symdir directories)
│   └── TI-TPAxxx_AUDIO-AMP.kicad_symdir/
├── footprints/                 # Footprint libraries (*.pretty directories)
│   └── TI-TPAxxx_AUDIO-AMP.pretty/
├── 3dmodels/                   # 3D STEP / WRL models (*.3dshapes directories)
│   └── TI-TPAxxx_AUDIO-AMP.3dshapes/
├── template/                   # KiCad project templates and page layouts (*.kicad_wks)
├── staging-temp/               # Scratchpad intake folder (ignored by Git)
│   ├── intake/                 # Drop new parts here for batch processing
│   └── imported_archive/       # Local archive of raw downloads
└── scripts/
    ├── lib_manager.py          # Unified CLI / GUI launcher
    └── src/                    # Python core engine and Tkinter GUI
```

---

## Quickstart: Setup on a New Machine

### 1. Clone the Library
```bash
git clone https://github.com/mraxxi/kicad_customlib.git /path/to/KICAD_CUSTOM_LIB
```

### 2. Configure Path Variable in KiCad
1. Open KiCad.
2. Go to **Preferences** -> **Configure Paths...**
3. Add a new variable:
   - **Name**: `KICAD_CUSTOM_LIB`
   - **Path**: `/path/to/KICAD_CUSTOM_LIB` (absolute path to this folder).

### 3. Add Master Tables to Global Libraries
1. In KiCad, go to **Preferences** -> **Manage Symbol Libraries...**
   - In the **Global Libraries** tab, click the folder icon to add a library.
   - Select `sym-lib-table` inside `KICAD_CUSTOM_LIB/`.
   - Set Library Format to `Table` (or let KiCad detect it).
2. Go to **Preferences** -> **Manage Footprint Libraries...**
   - In the **Global Libraries** tab, add `fp-lib-table` inside `KICAD_CUSTOM_LIB/` (Format: `Table`).

*Done! All current and future custom components will automatically appear in your KiCad projects.*

---

## Using the Library Manager

### Launching the GUI
```bash
python scripts/lib_manager.py
```

### Command Line Interface (CLI)

#### 1. Ingest a New Component from ZIP or Folder
```bash
python scripts/lib_manager.py ingest /path/to/downloaded_part.zip --category TI-TPAxxx_AUDIO-AMP --part TPA3155DDV
```

#### 2. Process all parts dropped in `staging-temp/intake/`
```bash
python scripts/lib_manager.py sync-staging
```

#### 3. Move / Reorganize a Part into a New Category
```bash
python scripts/lib_manager.py move --part TPA3255 --to TI-TPAxxx_AUDIO-AMP
```

#### 4. Regenerate Master Tables
```bash
python scripts/lib_manager.py generate
```

#### 5. Audit Library Health (Missing 3D models, unlinked footprints)
```bash
python scripts/lib_manager.py check
```

#### 6. Export / Package a Self-Contained Project Library
```bash
python scripts/lib_manager.py package /path/to/my_project --out /path/to/my_project/project_libs
```

---

## Adding 3D Model Offsets
To adjust 3D alignment permanently:
1. Open KiCad **Footprint Editor**.
2. Open the custom footprint (e.g., `TI-TPAxxx_AUDIO-AMP:SOP63P810X120-44N`).
3. Press `E` (Properties) -> **3D Models** tab.
4. Adjust Offset (X/Y/Z) and Rotation until the model aligns with pads.
5. Save. The offset is stored in the `.kicad_mod` file and will automatically apply anywhere this footprint is used.
