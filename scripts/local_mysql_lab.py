"""Isolated, bounded native MySQL primary/replica validation (no PQ required).

Run with the repository venv. Credentials are generated into a private ignored
file; this script never connects to an existing MySQL service. All server
settings are written before initialization, never changed while testing.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time

import mysql.connector
import yaml


ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / ".local" / "mysql-validation"
STATE = LAB / "private-state.json"
ROLES = ("baseline", "custom_off", "custom_on")


def connect(state, index):
    return mysql.connector.connect(
        host="127.0.0.1", port=state["instances"][index]["port"],
        user="sf_lab", password=state["password"], autocommit=True,
        connection_timeout=3, read_timeout=3, write_timeout=3, use_pure=True,
    )


def fetch(connection, sql):
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(sql)
        return cursor.fetchall() if cursor.with_rows else []
    finally:
        cursor.close()


def read_state():
    return json.loads(STATE.read_text())


def start(base_port, binary):
    existing = read_state() if STATE.exists() else None
    if existing is not None:
        base_port = existing["instances"][0]["port"]
        binary = existing["binary"]
    binary = shutil.which(binary)
    if not binary:
        raise SystemExit("mysqld is not installed; supply --mysqld /path/to/mysqld")
    # Reserve all six ports before creating anything; never attach to an existing service.
    reserved = []
    try:
        for port in range(base_port, base_port + 6):
            listener = socket.socket()
            listener.bind(("127.0.0.1", port))
            reserved.append(listener)
    finally:
        for listener in reserved:
            listener.close()
    LAB.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = existing or {"password": secrets.token_hex(12), "instances": [], "binary": binary}
    for index in (() if existing else range(6)):
        directory = LAB / f"node-{index}"
        directory.mkdir()
        data = directory / "data"
        config = directory / "my.cnf"
        port = base_port + index
        sock = LAB / f"n{index}.sock"
        config.write_text("\n".join([
            "[mysqld]", f'port={port}', 'bind-address=127.0.0.1',
            f'datadir="{data}"', f'socket="{sock}"',
            f'pid-file="{directory / "mysqld.pid"}"',
            f'log-error="{directory / "error.log"}"',
            f'server-id={base_port + index}', 'mysqlx=OFF', 'skip-name-resolve=ON',
            'gtid-mode=ON', 'enforce-gtid-consistency=ON',
            f'log-bin="{directory / "binlog"}"', 'binlog-format=ROW',
            f'relay-log="{directory / "relaylog"}"', 'relay-log-recovery=ON',
            'binlog-expire-logs-seconds=86400', 'max-connections=80',
            'innodb-buffer-pool-size=128M', 'innodb-redo-log-capacity=64M',
            'slow-query-log=ON', 'long-query-time=1',
            f'slow-query-log-file="{directory / "slow.log"}"',
            f'read-only={"ON" if index % 2 else "OFF"}', "",
        ]))
        state["instances"].append({
            "port": port, "socket": str(sock), "directory": str(directory),
            "config": str(config), "role": ROLES[index // 2],
            "endpoint": "replica" if index % 2 else "primary",
        })
    if existing is None:
        descriptor = os.open(STATE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output, indent=2)

    def initialize(index):
        node = state["instances"][index]
        with (Path(node["directory"]) / "bootstrap.log").open("ab") as output:
            if not (Path(node["directory"]) / "data" / "auto.cnf").exists():
                subprocess.run(
                    [binary, f'--defaults-file={node["config"]}', "--initialize-insecure"],
                    stdout=output, stderr=output, check=True, timeout=120,
                )
            subprocess.Popen(
                [binary, f'--defaults-file={node["config"]}'],
                stdout=output, stderr=output, start_new_session=True,
            )
        deadline = time.monotonic() + 45
        while True:
            connection = None
            # An interrupted bootstrap can leave either credential state. Only
            # try these two known credentials on this lab's private Unix socket.
            for root_password in (state["password"], "") if existing else ("",):
                try:
                    connection = mysql.connector.connect(
                        unix_socket=node["socket"], user="root", autocommit=True,
                        password=root_password,
                        connection_timeout=2, read_timeout=3, write_timeout=3, use_pure=True,
                    )
                    break
                except mysql.connector.Error:
                    continue
            if connection is not None:
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f'MySQL failed to start at port {node["port"]}') from None
            time.sleep(0.25)
        try:
            cursor = connection.cursor()
            # IF NOT EXISTS makes identical bootstrap accounts safe to replay via GTID.
            cursor.execute(
                "CREATE USER IF NOT EXISTS 'sf_lab'@'127.0.0.1' IDENTIFIED BY %s",
                (state["password"],),
            )
            cursor.execute("GRANT ALL PRIVILEGES ON *.* TO 'sf_lab'@'127.0.0.1' WITH GRANT OPTION")
            cursor.execute(
                "CREATE USER IF NOT EXISTS 'sf_repl'@'127.0.0.1' IDENTIFIED BY %s",
                (state["password"][:24],),
            )
            cursor.execute("ALTER USER 'sf_repl'@'127.0.0.1' IDENTIFIED BY %s",
                           (state["password"][:24],))
            cursor.execute("GRANT REPLICATION SLAVE ON *.* TO 'sf_repl'@'127.0.0.1'")
            cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (state["password"],))
            cursor.close()
        finally:
            connection.close()
        print(f'Ready: {node["role"]} {node["endpoint"]} 127.0.0.1:{node["port"]}', flush=True)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(initialize, range(6)))
        for index in (1, 3, 5):
            connection = connect(state, index)
            try:
                cursor = connection.cursor()
                cursor.execute("STOP REPLICA")
                cursor.execute(
                    "CHANGE REPLICATION SOURCE TO SOURCE_HOST='127.0.0.1', "
                    "SOURCE_PORT=%s, SOURCE_USER='sf_repl', SOURCE_PASSWORD=%s, "
                    "SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1",
                    (state["instances"][index - 1]["port"], state["password"][:24]),
                )
                cursor.execute("START REPLICA")
                cursor.close()
            finally:
                connection.close()
        deadline = time.monotonic() + 30
        while True:
            samples = [snapshot(state, i) for i in (1, 3, 5)]
            if all(item["replication"].get("Replica_IO_Running") == "Yes"
                   and item["replication"].get("Replica_SQL_Running") == "Yes" for item in samples):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Replication bootstrap failed; inspect status and error logs")
            time.sleep(0.5)
        print("Three GTID primary/replica pairs ready. Credentials remain in private-state.json.")
    except BaseException:
        stop(state)
        raise


def snapshot(state, index):
    node = state["instances"][index]
    result = {"port": node["port"], "role": node["role"], "endpoint": node["endpoint"]}
    connection = connect(state, index)
    try:
        result["processlist"] = fetch(connection, "SHOW FULL PROCESSLIST")
        result["status"] = fetch(connection,
            "SHOW GLOBAL STATUS WHERE Variable_name IN ('Com_select','Com_insert',"
            "'Com_update','Com_delete','Com_replace','Threads_connected','Threads_running',"
            "'Slow_queries','Aborted_clients','Aborted_connects')")
        replication = fetch(connection, "SHOW REPLICA STATUS")
        result["replication"] = {
            key: replication[0].get(key) for key in (
                "Replica_IO_Running", "Replica_SQL_Running", "Seconds_Behind_Source",
                "Last_IO_Errno", "Last_IO_Error", "Last_SQL_Errno", "Last_SQL_Error",
                "Retrieved_Gtid_Set", "Executed_Gtid_Set",
            )
        } if replication else {}
    finally:
        connection.close()
    return result


def stop(state):
    pending = []
    errors = []
    for node in state["instances"]:
        pid_file = Path(node["directory"]) / "mysqld.pid"
        if not pid_file.exists():
            continue
        pid = int(pid_file.read_text().strip())
        command = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True).stdout
        if not command.strip():
            continue  # A crash may leave a PID file even though the process is gone.
        if f'--defaults-file={node["config"]}' not in command:
            errors.append(f"Refusing to signal unrecognized PID {pid}")
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            pending.append((pid, pid_file))
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 45
    while pending:
        active = []
        for pid, pid_file in pending:
            if not pid_file.exists():
                continue
            try:
                os.kill(pid, 0)
                active.append((pid, pid_file))
            except ProcessLookupError:
                pass
        pending = active
        if not pending:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Shutdown has not finished; data preserved, inspect instance logs")
        time.sleep(0.25)
    if errors:
        raise RuntimeError("; ".join(errors))
    print("Lab instances stopped; data and evidence preserved.")


def config_for(state, mode, profile):
    config = {
        "mode": mode, "full_thread_sql_log": True, "replica_parameters_file": None,
        "replica_sync_timeout_seconds": 20,
        "nodes": [{"role": role, **{
            endpoint: {"host": "127.0.0.1", "port": state["instances"][2 * i + j]["port"]}
            for j, endpoint in enumerate(("primary", "replica"))
        }} for i, role in enumerate(ROLES)],
        "correctness": {
            "workers": 2, "queries_per_round": 40, "timeout_seconds": 5,
            "min_tables": 2, "max_tables": 3, "min_columns": 8, "max_columns": 20,
            "max_query_tables": 3, "min_rows_per_table": 50, "max_rows_per_table": 200,
        },
        "performance": {
            "queries_per_round": 10, "initial_table_rows": 2000,
            "initial_table_rows_max": 5000, "max_table_rows": 10000, "max_total_rows": 30000,
            "insert_batch_rows": 500, "min_tables": 2, "max_tables": 3,
            "min_columns": 6, "max_columns": 12, "max_query_tables": 3,
            "formal_timeout_seconds": 5, "materialization_timeout_seconds": 60,
        },
        "fuzz": {
            "databases": 1, "writer_threads_per_database": 1, "reader_threads_per_database": 3,
            "max_total_connections": 64, "initial_tables": 2, "initial_rows_per_table": 200,
            "max_rows_per_database": 20000, "min_columns_per_table": 50,
            "max_columns_per_table": 80, "min_indexes_per_table": 4, "max_indexes_per_table": 6,
            "query_timeout_seconds": 3, "batch_rows_min": 10, "batch_rows_max": 25,
            "delete_batch_rows_min": 1, "delete_batch_rows_max": 10,
            "query_generator_processes": 1, "schema_refresh_interval_seconds": 30,
            "diagnostics_interval_seconds": 2,
        },
    }
    if profile == "wide":
        config["fuzz"].update({
            "databases": 2, "writer_threads_per_database": 2, "reader_threads_per_database": 6,
            "initial_tables": 3, "initial_rows_per_table": 500,
            "min_columns_per_table": 200, "max_columns_per_table": 500,
            "batch_rows_min": 25, "batch_rows_max": 100,
            "query_generator_processes": 2, "schema_refresh_interval_seconds": 45,
        })
    elif profile == "wide-bounded":
        config["fuzz"].update({
            "databases": 2, "writer_threads_per_database": 2, "reader_threads_per_database": 6,
            "initial_tables": 3, "initial_rows_per_table": 200,
            "max_rows_per_database": 2000,
            "min_columns_per_table": 200, "max_columns_per_table": 500,
            "batch_rows_min": 1, "batch_rows_max": 5,
            "query_generator_processes": 2, "schema_refresh_interval_seconds": 40,
        })
    return config


def run(state, args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    config_path = output / "config.yaml"
    configuration = config_for(state, args.mode, args.profile)
    configuration["fuzz"]["connector_implementation"] = args.connector
    config_path.write_text(yaml.safe_dump(configuration, sort_keys=False))
    environment = dict(os.environ, SELECT_FUZZ_MYSQL_USER="sf_lab",
                       SELECT_FUZZ_MYSQL_PASSWORD=state["password"])
    cli = [str(Path(sys.executable).parent / "select-fuzz")]
    with (output / "doctor.json").open("w") as doctor:
        subprocess.run(cli + ["doctor", "--mode", args.mode, "--config", str(config_path)],
                       env=environment,
                       stdout=doctor, stderr=subprocess.STDOUT, check=True, timeout=45)
    command = cli + ["run", "--mode", args.mode, "--config", str(config_path),
                     "--rounds", str(args.rounds),
                     "--seed", str(args.seed), "--duration-seconds", str(args.seconds),
                     "--artifacts", str(output / "run")]
    indices = (4, 5) if args.mode == "fuzz" else tuple(range(6))
    variables = {}
    for index in indices:
        connection = connect(state, index)
        try:
            variables[index] = fetch(connection, "SHOW GLOBAL VARIABLES")
        finally:
            connection.close()
    (output / "variables-before.json").write_text(json.dumps(variables, indent=2, default=str))
    started = time.monotonic()
    interrupted_at = None
    with (output / "stdout.json").open("w") as stdout, (output / "stderr.log").open("w") as stderr:
        process = subprocess.Popen(command, env=environment, stdout=stdout, stderr=stderr)
        (output / "pid").write_text(str(process.pid))
        try:
            with (output / "processlist.jsonl").open("w") as evidence:
                while True:
                    for index in indices:
                        try:
                            sample = snapshot(state, index)
                        except Exception as error:
                            sample = {"port": state["instances"][index]["port"],
                                      "error": f"{type(error).__name__}: {error}"}
                        sample["elapsed_seconds"] = round(time.monotonic() - started, 3)
                        evidence.write(json.dumps(sample, default=str) + "\n")
                    evidence.flush()
                    if process.poll() is not None:
                        break
                    if (args.interrupt_after is not None and interrupted_at is None
                            and time.monotonic() - started >= args.interrupt_after):
                        process.send_signal(signal.SIGINT)
                        interrupted_at = round(time.monotonic() - started, 3)
                    if time.monotonic() - started > args.seconds + 60:
                        raise TimeoutError("Tool did not stop within its duration plus 60 seconds")
                    time.sleep(1)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    after = {}
    for index in indices:
        connection = connect(state, index)
        try:
            after[index] = fetch(connection, "SHOW GLOBAL VARIABLES")
        finally:
            connection.close()
    (output / "variables-after.json").write_text(json.dumps(after, indent=2, default=str))
    # GTID sets report transaction progress; they are not tuning changes.
    changes = {}
    for index, rows in variables.items():
        before_values = {row["Variable_name"]: row["Value"] for row in rows}
        changes[index] = [row["Variable_name"] for row in after[index]
                          if row["Variable_name"] not in {
                              "gtid_executed", "gtid_purged", "gtid_owned",
                          }
                          and before_values.get(row["Variable_name"]) != row["Value"]]
    result = {"mode": args.mode, "exit_code": process.returncode,
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "server_parameters_unchanged": not any(changes.values()),
              "changed_parameters": changes, "interrupted_at_seconds": interrupted_at,
              "output": str(output)}
    (output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop", "run"))
    parser.add_argument("--base-port", type=int, default=33981)
    parser.add_argument("--mysqld", default="mysqld")
    parser.add_argument("--mode", choices=("correctness", "performance", "fuzz"), default="fuzz")
    parser.add_argument("--profile", choices=("smoke", "wide", "wide-bounded"), default="smoke")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260908)
    parser.add_argument("--connector", choices=("auto", "c", "python"), default="auto")
    parser.add_argument("--interrupt-after", type=float)
    parser.add_argument("--output", default="artifacts/local-mysql-20260908/fuzz-smoke")
    args = parser.parse_args()
    if not 1024 <= args.base_port <= 65530 or not 1 <= args.seconds <= 1800 or not 1 <= args.rounds <= 100:
        parser.error("ports must be 1024..65530, seconds 1..1800, rounds 1..100")
    if args.interrupt_after is not None and not 0 < args.interrupt_after < args.seconds:
        parser.error("interrupt-after must be positive and less than seconds")
    if args.action == "start":
        start(args.base_port, args.mysqld)
    elif args.action == "stop":
        stop(read_state())
    elif args.action == "status":
        state = read_state()
        observations = []
        for index, node in enumerate(state["instances"]):
            try:
                observations.append(snapshot(state, index))
            except mysql.connector.Error as error:
                observations.append({"port": node["port"], "role": node["role"],
                                     "endpoint": node["endpoint"], "reachable": False,
                                     "error": f"{type(error).__name__}: errno={error.errno}"})
        print(json.dumps(observations, indent=2, default=str))
    else:
        run(read_state(), args)


if __name__ == "__main__":
    main()
