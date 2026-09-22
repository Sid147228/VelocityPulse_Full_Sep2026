import csv, random, time

# Config
rows = 50000
transactions = ["Login", "Search", "Checkout", "Logout", "Browse"]
start_ts = int(time.time() * 1000)

with open("synthetic_jmeter_results.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow([
        "timeStamp","elapsed","label","responseCode","responseMessage",
        "success","threadName","dataType","bytes","grpThreads","allThreads",
        "Latency","IdleTime","Connect"
    ])

    for i in range(rows):
        ts = start_ts + i * 100  # spread timestamps
        label = random.choice(transactions)

        # Response times vary by transaction
        base = {"Login": 300, "Search": 500, "Checkout": 800, "Logout": 200, "Browse": 400}[label]
        elapsed = int(random.gauss(base, base * 0.2))  # normal distribution

        # Success vs failure
        if random.random() < 0.95:
            code, msg, success = 200, "OK", "true"
        else:
            code, msg, success = 500, "Internal Server Error", "false"

        thread_id = random.randint(1, 200)
        threadName = f"Thread Group 1-{thread_id}"
        bytes_sent = random.randint(500, 5000)
        grpThreads = allThreads = random.randint(50, 200)
        latency = int(elapsed * random.uniform(0.5, 0.9))
        idle = 0
        connect = int(elapsed * random.uniform(0.1, 0.3))

        writer.writerow([
            ts, elapsed, label, code, msg, success, threadName, "text",
            bytes_sent, grpThreads, allThreads, latency, idle, connect
        ])

print("✅ synthetic_jmeter_results.csv generated with", rows, "rows")