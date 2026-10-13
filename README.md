# vehicle-api

Spinny-style vehicle lead API (Flask, Vercel-ready), backed by live RC sources
(SMC Insurance, ACKO VehicleInfo, VahanX, vahandetails).

## Run

```cmd
cd /d "C:\Users\JESHAN KHALKHO\Desktop\vehicle-api"
pip install -r requirements.txt
python api\index.py
```

Server listens on `http://localhost:5001` (`PORT` env overrides).

## Endpoints

```text
GET  /sp-insurance-platform/api/v1/leads/get-lead-by-registration-or-proposal/?car_registration_number=JH05CK2458
POST /sp-insurance-platform/api/v1/leads/get-or-create-lead/
GET  /health
GET  /
```

Create-a-lead body (JSON):

```json
{
  "car_registration_number": "JH05CK2458",
  "name": "Amit Kumar",
  "mobile": "9876543210",
  "email": "amit@example.com",
  "source": "website"
}
```

`POST` returns `{"status": "success", "created": true|false, "lead": {...}}` —
same lead is returned on repeat calls (get-or-create).

## Test (cmd)

```cmd
curl "http://localhost:5001/sp-insurance-platform/api/v1/leads/get-lead-by-registration-or-proposal/?car_registration_number=JH05CK2458"

curl -X POST http://localhost:5001/sp-insurance-platform/api/v1/leads/get-or-create-lead/ ^
  -H "Content-Type: application/json" ^
  -d "{\"car_registration_number\":\"JH05CK2458\",\"name\":\"Test\",\"mobile\":\"9999999999\"}"
```

## Notes

- Response field names are an assumed Spinny-like shape (the real Spinny
  response JSON was not available). Paste the real `Preview` JSON from
  DevTools and the envelope will be aligned to it.
- Leads persist in `api/leads.json` (git-ignored). Ephemeral on
  serverless — use a real DB for production.
- Rate limit: 10 req/min per IP on the lead endpoints.
