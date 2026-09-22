import csv, random, time, os

# Config
rows = 50000
transactions = ["Login", "Search", "Checkout", "Logout", "Browse"]
start_ts = int(time.time() * 1000)

# Ensure output directory
os.makedirs("synthetic_runs", exist_ok=True)

for run in range(1, 16):
    filename = f"synthetic_runs/synthetic_jmeter_results_run{run:02d}.csv"
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timeStamp","elapsed","label","responseCode","responseMessage",
            "success","threadName","dataType","bytes","grpThreads","allThreads",
            "Latency","IdleTime","Connect"
        ])

        # Vary base response times slightly per run (±20%)
        base_multiplier = random.uniform(0.8, 1.2)
        # Vary error rate per run (5–10%)
        base_error_rate = random.uniform(0.05, 0.10)

        for i in range(rows):
            ts = start_ts + i * 100
            label = random.choice(transactions)

            base = {
                "Login": 1200,
                "Search": 2000,
                "Checkout": 3000,
                "Logout": 800,
                "Browse": 1500
            }[label] * base_multiplier

            # Normal distribution around the adjusted base
            elapsed = int(random.gauss(base, base * 0.25))

            # Inject occasional extreme spikes
            if i % 1500 == 0:
                elapsed *= random.randint(4, 8)

            # Success vs failure
            if random.random() < (1 - base_error_rate):
                code, msg, success = 200, "OK", "true"
            else:
                code, msg, success = 500, "Internal Server Error", "false"

            thread_id = random.randint(1, 200)
            threadName = f"Thread Group 1-{thread_id}"
            bytes_sent = random.randint(500, 5000)

            grpThreads = allThreads = 200
            latency = int(elapsed * random.uniform(0.6, 0.95))
            idle = 0
            connect = int(elapsed * random.uniform(0.2, 0.4))

            writer.writerow([
                ts, elapsed, label, code, msg, success, threadName, "text",
                bytes_sent, grpThreads, allThreads, latency, idle, connect
            ])

    print(f"✅ {filename} generated with {rows} rows (multiplier={base_multiplier:.2f}, error_rate={base_error_rate:.2%})")
