import sys
import os

sys.path.append(os.getcwd())

from ptbrush.db import SystemMessage, database

print(f"DB Path: {database.database}")
if os.path.exists(database.database):
    print("DB file exists.")
else:
    print("DB file MISSING!")

try:
    # Test creation
    SystemMessage.create(
        message_type="INFO", category="SYSTEM", content="Debug log test"
    )
    print("Created test log")

    count = SystemMessage.select().count()
    print(f"SystemMessage count: {count}")

    msgs = SystemMessage.select().order_by(SystemMessage.created_time.desc()).limit(5)
    for m in msgs:
        print(f"[{m.created_time}] {m.message_type}: {m.content}")
except Exception as e:
    print(f"Error: {e}")
