import sys
sys.path.append('.')
from modules.database import db
import json

if db:
    tracking = db.select('email_tracking', order='opened_at.desc', limit=10)
    print(json.dumps(tracking, indent=2, default=str))
else:
    print("No database connection")
