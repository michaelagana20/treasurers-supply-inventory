# Treasurer's Supply Inventory

Local, offline-first supply inventory for the Treasurer's Office, care of Samboy. Version 1.0.0 uses only Python's standard library and SQLite. It binds to `127.0.0.1`, so it is not exposed to the local network or internet.

## Install and start on Windows

1. Install Python 3.10 or newer from python.org. During installation, enable **Add Python to PATH**.
2. Copy this application folder to a stable location such as `C:\TreasurersSupplyInventory`.
3. Double-click `launcher.bat`. The default browser opens automatically.
4. Keep the terminal window open while using the app. Close it or press Ctrl+C to stop the server.

No internet connection or package installation is required. By default the database is stored at `%LOCALAPPDATA%\TreasurersSupplyInventory\inventory.db`, outside the application folder, so replacing application files during an update does not overwrite records.

To make a desktop shortcut, right-click `launcher.bat`, choose **Show more options > Send to > Desktop (create shortcut)**, then rename it to **Supply Inventory**.

Advanced options:

```powershell
py -3 server.py --port 8765
$env:TSI_DATA_DIR='D:\InventoryData'; py -3 server.py
```

## Everyday use

- **Configuration:** Create a supply with direct stock, or check “Create with variants” and add named variants with separate starting quantities. Starting quantities and every later addition are recorded as dated additions. A direct supply with any stock or transaction history cannot be converted to variants; create the correctly structured supply instead. This prevents accidental duplication or loss.
- **Request:** Enter or reuse a requestor name and add multiple direct or variant items.
- **Requested:** Review each request and enter actual released quantities. Inventory changes only when **Record release** is selected. Zero is allowed when an item was not issued. A release cannot exceed current stock.
- **Released:** Review permanent release records. Use **Return Quantity** on a released line to correct an accidental deduction. Positive quantity and explanatory remarks are required, and cumulative returns cannot exceed the original release.
- **Dashboard:** See total additions, net use, balance, percentages, variant breakdowns, and recent activity. “Used” is releases minus corrections.

Quantities are whole units in version 1. If an office stocks packs and pieces separately, configure those as distinct variants.

## Backup and restore

Open **Settings > Backup & Restore**. **Create database backup** makes a timestamped, transaction-safe copy in `%LOCALAPPDATA%\TreasurersSupplyInventory\backups`.

To restore, choose a backup, click **Restore**, and type `RESTORE`. The selected database is integrity-checked first. Immediately before replacement, the app creates an additional `before-restore-...db` safety copy beside the live database. Stop other copies of the app before restoring.

For protection against computer or drive failure, periodically copy the entire `%LOCALAPPDATA%\TreasurersSupplyInventory` folder to an approved external drive. Treat backups as confidential office records.

## Updating without losing data

The installed version appears in **Settings**. The Windows launcher checks published releases from `michaelagana20/treasurers-supply-inventory` when the user selects **Check for updates**. The check only runs on demand. If offline, it fails harmlessly and all inventory features continue working.

Before updating:

1. Create a manual backup and stop the app.
2. Replace the application/code folder with the downloaded release.
3. Do not remove `%LOCALAPPDATA%\TreasurersSupplyInventory`.
4. Start the new version. It reuses the database in that separate data directory.

Version 1's update notice opens the GitHub release download page; it never installs an update automatically.

## Data model and audit behavior

SQLite stores supplies, internal direct/variant inventory items, stock additions, requests, releases, and corrections. The UI never shows its internal direct-stock marker as a fake variant. Current stock is computed as `additions - releases + corrections`; original release rows are retained and corrections are appended with their timestamp and remarks.

## Troubleshooting

- If the browser does not open, visit `http://127.0.0.1:8765`.
- If port 8765 is in use, run `py -3 server.py --port 8766` and visit that port.
- If Python is not found, reinstall Python and enable the PATH option.
- The terminal displays local request logs and the exact database path at startup.
