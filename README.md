# Employee Attendance and Analytics API

A FastAPI and MongoDB service for employee attendance, manual corrections, and reporting. MongoDB indexes are created when the app starts.

## Run locally

Use Python 3.11+ and MongoDB 6.0+. Set `MONGO_URI` and `MONGO_DB` in your environment, install the requirements, then run:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Configure MONGO_URI and MONGO_DB in the shell before starting the app.
uvicorn app.main:app --port 8000
```

The service exposes `/health`, employee and attendance operations, analytics endpoints, and `/admin/explain/{endpoint}`. Open `http://localhost:8000/docs` to inspect and try the API.

## Notes

The API is implemented in `app/main.py`. It stores instants as MongoDB datetimes and returns epoch milliseconds. Do not commit local environment files, credentials, or database exports.




