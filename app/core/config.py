GCP_PROJECT_ID = "pambii-ai-inc"
GCP_REGION = "us-central1"
SERVICE_NAME = "crewlee-api"
CLOUD_SQL_INSTANCE = "pambii-ai-inc:us-central1:crewlee"
DB_NAME = "crewlee"
DB_TABLE = "waitlist"

PROJECT_NAME = "Crewlee"
PROJECT_SLUG = "crewlee"

DB_FIELDS = [
    {"name": "name",       "label": "Your Name",       "type": "text",   "required": True},
    {"name": "email",      "label": "Email",           "type": "email",  "required": True},
    {"name": "restaurant", "label": "Restaurant Name", "type": "text",   "required": True},
    {
        "name": "role", "label": "Your Role", "type": "select", "required": True,
        "selectOptions": ["Owner", "General Manager", "Operations Manager", "Other"],
    },
]
