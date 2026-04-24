from modules.database import db
if db:
    leads = db.select('leads', columns='id,contact_email', limit=5000)
    to_del = [l['id'] for l in leads if l.get('contact_email') != 'githubthe12@gmail.com']
    for i in range(0, len(to_del), 50):
        batch = to_del[i:i+50]
        db.delete('leads', {'id': f"in.({','.join([str(x) for x in batch])})"})
    remaining = db.select('leads', columns='id,contact_email', limit=100)
    print(f"Deleted {len(to_del)} leads. Remaining: {len(remaining)}")
    for r in remaining:
        print(f"  - {r.get('contact_email')}")
