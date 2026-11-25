from pathlib import Path

# Project root is the parent of src/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
DATA_RAW = DATA_DIR / "raw"
DATA_PROCESSED = DATA_DIR / "processed"

OUTPUTS_DIR = PROJECT_ROOT / "outputs"

# Raw input files
PRICE_EXPIRATION_FILE = DATA_RAW / "20260930_energy_futures_price_and_expiration.xlsx"

# Processed files (output of process_data.py)
PROCESSED_DATA_FILE = DATA_PROCESSED / "processed_data.csv"
CFTC_COT_FILE = DATA_PROCESSED / "cftc_cot.csv"
CFTC_DCOT_FILE = DATA_PROCESSED / "cftc_dcot.csv"
EXPIRATION_FILE = DATA_PROCESSED / "expiration.csv"
POSITIONS_FILE = DATA_PROCESSED / "positions.csv"

# Inclusive reporting period; earlier data is used to initialize rolls and signals.
PERIOD_START = "2012-01-01"
PERIOD_END = "2026-08-31"

# Short and long moving-average windows in weekly reports.
SHORT_WINDOWS = range(1, 5)
LONG_WINDOWS = range(8, 27)

# Independent simulations and seed for reproducible random weekly window choices.
MONTE_CARLO_TRIALS = 1_000_000
RANDOM_SEED = 42

# Costs per barrel per executed contract leg; each contract represents 1,000 barrels.
TRANSACTION_COSTS = (0.0, 0.02, 0.05, 0.10, 0.20)
IRAN_WAR_WINDOW_START = "2026-03-01"