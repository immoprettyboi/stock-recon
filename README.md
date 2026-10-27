# Stock Recon

Takealot stock and sales tracker.

- **App** (this folder): open https://immoprettyboi.github.io/stock-recon/ and sign in. Install it from Chrome's menu (PC: *Install Stock Recon*; Android: *Add to Home screen*).
- **Sync** (`sync/`): runs on the office PC. Opens Chrome on the Takealot Seller Portal every 30 minutes and matches sales to stock. See `sync/README.txt`.
- **Database** (`database/`): Supabase setup scripts.

Stock data lives in Supabase behind a login. Nothing in this repository gives access to it.
