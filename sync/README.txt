STOCK RECON - TAKEALOT SYNC
===========================

What it does
  Every 30 minutes it opens Chrome in the background on the Takealot Seller
  Portal, clicks "Export Sales Details", and matches each sale to a unit in
  your Stock Recon app: order ID, sold date, gross sale and fees.

Matching rules
  1. Unit already has this order ID           -> update fees/status
  2. Sale has a PO number                      -> unsold unit with that PO,
                                                  else unsold unit at HOME
  3. No PO                                     -> unsold unit at the same
                                                  warehouse (JHB/CPT/DBN)
  Always the unit that arrived first. Anything it can't place shows under
  "Needs matching" in the app. Cancelled orders put the unit back in stock.
  Returns are flagged so you can choose: back in Takealot stock, or coming
  back to you.

Files
  INSTALL.bat   install once
  config.ini    paste your Supabase service key here (keep it private)
  LOGIN.bat     log into the Seller Portal (once, or when Takealot logs you out)
  SYNC_NOW.bat  run one sync with Chrome visible
  START.bat     start the background sync
  STOP.bat      stop the background sync
  sync_log.txt  what happened on each run

Chrome profile: %LOCALAPPDATA%\StockRecon\chrome-profile
(separate from your normal Chrome, so it never touches your own tabs)
