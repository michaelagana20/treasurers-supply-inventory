# Treasurer's Supply Inventory

Treasurer's Supply Inventory is a local, offline-first inventory application for the Treasurer's Office, maintained for Samboy. It runs on one Windows computer, opens in a browser, stores its records in SQLite, and does not require a cloud database.

The local server binds only to `127.0.0.1`. Other computers on the network cannot access it unless the application is deliberately modified.

## Main features

- Main supplies with optional variants and optional detailed variant names
- Direct stock for supplies that do not use variants
- Dated stock-addition history instead of quantity replacement
- Bulk supply entry for faster initial setup
- Reusable requestor names and searchable supply selection
- Multiple supply lines in one request
- Pending requests and new requests in one workspace
- Actual release quantities with an available-stock review before confirmation
- Protection against releases that exceed current stock
- Return Quantity corrections with required remarks and permanent history
- Supply and variant archiving with inventory safeguards
- Restoration of archived variants
- Dashboard stock warnings, sorting, search, pagination, and recent activity
- Complete read-only Activity Log with detailed side-panel views
- Formatted Excel reports with historical date filtering
- Manual SQLite backup, validated restore, and database reset with safety backups
- Light and dark color modes
- Offline daily operation and optional GitHub Release update checks

## Navigation

### Dashboard

The Dashboard provides the daily overview:

- Pending requests
- Supplies currently available
- Supplies needing attention
- Out-of-stock supplies
- Supply and variant stock levels
- Remaining-stock warning colors and progress bars
- Search, low-stock sorting, and pagination
- The five most recent activities, with access to the full Activity Log

Inventory is calculated as:

```text
Remaining = stock additions - releases + corrections
Used = releases - corrections
```

### Request

The Request page combines request creation and pending-request management.

- Enter a new requestor or reuse a saved name.
- Search or open the supply-item dropdown. Results are limited on screen and remain scrollable.
- Select a main supply directly when it has no variants.
- Select the exact variant when variants are used.
- Add multiple items to one request.
- Review pending requests below the request form.
- Enter the actual quantities issued and select **Review & release**.

The confirmation dialog shows the quantity to release, current available stock, and expected stock after release. Inventory is deducted only after **Record release** is confirmed. The server rejects any release that exceeds available stock.

### Released

Released records are permanent and paginated at five releases per page. Staff can:

- Search by requestor or request number
- Sort newest or oldest first
- Filter by inclusive start and end dates
- Review requested, released, returned, and net-used quantities
- Use **Return quantity** to correct an accidental deduction

A correction requires a positive quantity and explanatory remarks. Total corrections cannot exceed the original released quantity. The original release is never silently changed or deleted.

### Activity Log

The Activity Log contains the full inventory audit history, including additions, requests, releases, corrections, archives, restores, and other recorded changes. It supports search, event-type filtering, pagination, and clickable detail panels.

### Reports

Reports are generated locally from the live SQLite database as real `.xlsx` files. Microsoft Excel is not required to generate them, but Excel or another compatible spreadsheet program is needed to open them.

Available reports:

- **Inventory Status:** added, released, returned, net used, remaining, percentage remaining, and status by supply item
- **Stock Additions:** every dated stock receipt with remarks
- **Requests and Releases:** requestor, status, dates, requested quantity, released quantity, returns, and net use
- **Correction History:** original release, quantity returned, requestor, remarks, and correction date
- **Complete Workbook:** summary plus all detailed report worksheets

Optional start and end dates filter transaction reports. Inventory Status and the summary are historical snapshots as of the selected end date. Supplies and transactions that did not yet exist on that date are excluded.

Excel exports include friendly, sortable date-time values, frozen headings, column filters, readable widths, percentage formatting, and generation details.

### Configuration

Configuration manages the supply catalog and stock.

- Create a supply with direct stock or with separately tracked variants.
- Give each variant an optional detailed name.
- Add stock to a direct supply or an individual variant.
- Search and paginate the supply catalog.
- Open a supply to manage stock and variants in the right-side panel.
- Search, sort, and paginate recent stock additions.
- Use bulk entry to create several supplies and variants efficiently.

Every quantity addition is appended to dated history. Existing stock is never silently replaced.

A supply with direct-stock history cannot be converted into variants. This prevents stock duplication or loss. Create a correctly structured new supply when a different tracking structure is required.

#### Archive rules

- A main supply or variant can be archived only when its remaining stock is zero.
- An item used by a pending request cannot be archived.
- Archive operations preserve transaction history.
- Archived variants can be viewed and restored from the parent supply panel.
- Restored variants return to active inventory and new-request selection.

### Settings

Settings shows the installed application version and provides:

- Manual update checks against the latest published GitHub Release
- Manual database backup
- Validated database restore with explicit confirmation
- Current database reset with the exact confirmation phrase `RESET DATABASE`

Reset creates a timestamped safety backup before clearing live records.

## Install and run on Windows

### Standalone application

1. Obtain the standalone `TreasurersSupplyInventory.exe` build from the application maintainer or a release that includes it.
2. Copy it into a stable writable folder, such as `C:\TreasurersSupplyInventory`.
3. Double-click `TreasurersSupplyInventory.exe`.
4. The application opens at `http://127.0.0.1:8765` in the default browser.

While running, the blue-and-yellow **TO** icon appears in the Windows notification area. It may be under the **^** hidden-icons button. Right-click it to open the app, open the data folder, configure startup behavior, or safely exit.

The tray menu can also enable **Run when Windows starts** and select whether startup opens a new tab, requests a new browser window, or starts quietly.

### Run from source

1. Install Python 3.10 or newer and enable **Add Python to PATH**.
2. Download or clone this repository.
3. Open the project folder.
4. Double-click `launcher.bat`, or run:

```powershell
py -3 server.py
```

The source version uses only Python's standard library. No package installation or internet connection is required for normal use.

Advanced examples:

```powershell
py -3 server.py --port 8766
$env:TSI_DATA_DIR='D:\InventoryData'; py -3 server.py
```

## Database and file locations

By default, the portable application keeps its working files beside the executable or source folder:

```text
TreasurersSupplyInventory\
├── TreasurersSupplyInventory.exe
├── data\
│   ├── inventory.db
│   └── tray-settings.json
└── backups\
    └── inventory-YYYYMMDD-HHMMSS.db
```

- Live database: `data\inventory.db`
- Tray preferences: `data\tray-settings.json`
- Manual backups: `backups\`

The database is SQLite on the Windows file system, not inside the browser. Keep the entire application folder in a stable writable location. Do not place the live database in Git, and do not commit the `data` or `backups` folders.

## Backup, restore, and reset

### Create a backup

Open **Settings** and select **Create database backup**. The app creates a timestamped, transaction-safe `.db` file in the visible `backups` folder shown on screen.

### Restore a backup

1. Open **Settings**.
2. Select a backup file.
3. Select **Restore**.
4. Type `RESTORE` to confirm.

The selected file is integrity-checked before use. The app creates an additional safety copy of the current live database before replacing it.

### Reset the database

Use **Reset database** only when all current records should be cleared. Type `RESET DATABASE` exactly. A safety backup is created automatically before supplies, requests, releases, corrections, and activity records are removed.

Periodically copy the `data` and `backups` folders to an approved external drive for protection against computer or drive failure. Treat all database copies as confidential office records.

## Update without losing records

The installed version and update checker are available in **Settings**. The checker contacts the configured GitHub repository only when requested. If the computer is offline, the check fails harmlessly and the app remains fully usable.

Before updating:

1. Create a manual backup.
2. Exit the application from the tray icon.
3. Replace the executable and application code with the newer release files.
4. Keep the existing `data` and `backups` folders unchanged.
5. Start the updated application.

Version 1 opens the GitHub Release page for download and does not install updates automatically.

## Build and publish a release

To rebuild the standalone executable on the configured development computer:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Build-only dependencies are stored in `.build-deps` and are not required by end users.

The GitHub Actions release workflow runs when a tag beginning with `v` is pushed. It verifies the Python source, runs workflow tests, creates a source-runtime ZIP containing `server.py`, `launcher.bat`, `README.md`, and the `web` folder, and publishes it as a GitHub Release asset. Users of that ZIP need Python 3.10 or newer. A standalone EXE can be built separately with `build_exe.ps1` and attached to the release.

Example:

```powershell
git tag v1.0.1
git push origin v1.0.1
```

## Safety and audit behavior

- The server listens on localhost only.
- Daily inventory work does not require internet access.
- SQLite foreign keys and validation protect related records.
- Releases cannot exceed available stock.
- Corrections cannot exceed their original released quantity.
- Original releases are retained after corrections.
- Stock additions are appended, never used to overwrite a balance.
- Archive actions do not delete transaction history.
- Restore and reset operations create safety copies.
- Activity records provide a permanent operational audit trail.

## Troubleshooting

- If the browser does not open, visit `http://127.0.0.1:8765`.
- If port `8765` is already in use, stop the other copy or run `py -3 server.py --port 8766`.
- If Python is not found, reinstall Python and enable its PATH option.
- If an update check fails, confirm internet access; inventory functions remain available offline.
- If Excel warns that a downloaded file is blocked, right-click the file, open **Properties**, select **Unblock** when available, and reopen it.
- Before restoring, close any second copy of the application that might be using the database.

## Technology

- Python standard-library HTTP server
- SQLite database
- Vanilla HTML, CSS, and JavaScript
- Locally generated Office Open XML (`.xlsx`) reports
- PyInstaller standalone Windows executable
