import datetime
import importlib
import inspect
import os
import sys
import time
import yaml

from dagster import asset_sensor, job, repository, schedule, sensor, build_resources, build_init_resource_context
from dagster import AssetKey, Backoff, RetryPolicy, RunRequest

from better_etl.resources.cache import cache

def build_job(job_conf):

    job_name = job_conf["name"]
    cache_conf = job_conf.get("cache", None)
    job_retry = job_conf.pop("retry", {})
    job_retry_max = job_retry.get("max", 0)
    job_retry_delay = job_retry.get("delay", 0)
    job_retry_backoff = job_retry.get("backoff", "linear")
    job_retry_lookback = job_retry.get("lookback_minutes", 0)

    ops_list = job_conf["ops"]
    ops_dict = {}
    job_conf = {"ops": {}}
    if cache_conf:
        job_conf["resources"] = {
            "cache": {
                "config" : cache_conf
            }
        }

    for op_conf in ops_list:
        if "config" not in op_conf:
            op_conf["config"] = {}

        op_conf["config"]["job_name"] = job_name
        job_conf["ops"][op_conf["name"]] = {"config": op_conf["config"]}
        ops_dict[op_conf["name"]] = op_conf

    op_packages = {}
    op_classes = {}
    op_metas = {}

    def dive(op_names, op_returns, depth):
        for op_name in op_names:
            # print(op_name)
            op_conf = ops_dict[op_name]
            package_name = op_conf["package"]
            if package_name not in op_packages:
                package_obj = importlib.import_module(package_name)
                op_packages[package_name] = package_obj

            class_name = op_conf["class"]
            full_class_name = f"{package_name}.{class_name}"
            if full_class_name not in op_classes:
                class_obj = getattr(op_packages[package_name], class_name)
                class_inst = class_obj()
                op_classes[full_class_name] = class_inst
            else:
                class_inst = op_classes[full_class_name]

            method_name = op_conf["method"]
            if method_name not in op_metas:
                # print(class_inst)
                # print(class_inst.get_op_metadata()[method_name])
                if hasattr(class_inst, "get_op_metadata"):
                    op_meta = class_inst.get_op_metadata()[method_name]
                else:
                    op_meta = {"return":{"dynamic":False}}
                op_metas[op_name] = op_meta

            if "after" not in op_conf:
                if op_name not in op_returns:
                    op = getattr(class_inst, method_name).alias(op_name)
                    r = op()
                    op_returns[op_name] = r
            else:
                after_list = op_conf["after"]
                dive(after_list, op_returns, depth + 1)
                if op_name not in op_returns:
                    cur_op = getattr(class_inst, method_name).alias(op_name)

                    cur_returns = []
                    for prev_name in after_list:
                        prev_return = op_returns[prev_name]
                        if op_metas[prev_name]["return"]["dynamic"]:
                            op_returns[op_name] = prev_return.map(cur_op).collect()
                            print(f"{op_name}.collect({prev_name})")
                            break
                        else:
                            cur_returns.append(prev_return)
                    if op_name not in op_returns:
                        op_returns[op_name] = cur_op(*cur_returns)

    @job(
        config=job_conf,
        name=job_name,
        resource_defs={
            "cache": cache
        },
        op_retry_policy=RetryPolicy(
            max_retries=job_retry_max,
            delay=job_retry_delay,
            backoff=Backoff(
                Backoff.EXPONENTIAL
                if job_retry_backoff == "exponential"
                else Backoff.LINEAR)
        )
    )
    def j():
        op_returns = {}
        dive(ops_dict.keys(), op_returns, 0)

    return job_conf, j, build_job_failure_sensor(j, job_retry_lookback, job_retry_max)


def build_sensor(job_conf, dagster_job_conf, job_func):

    job_name = job_conf["name"]

    @asset_sensor(name=job_name, asset_key=AssetKey(f"{job_name}_batch"), job=job_func)
    def s(context, asset_event):

        last_keys = asset_event.dagster_event.event_specific_data.materialization.metadata_entries[0].entry_data.data

        for op in dagster_job_conf["ops"].values():
            if "type" in op["config"] and op["config"]["type"] == "source":
                op["config"]["last_keys"] = last_keys

        return RunRequest(
            run_key=str(last_keys),
            run_config=dagster_job_conf,
        )

    return s


def build_schedule(job_conf, dagster_job_conf, job_func):

    schedule_conf = job_conf.get("schedule", None)

    if schedule_conf:

        schedule_ = str(job_conf["schedule"])

        @schedule(job=job_func, cron_schedule=schedule_)
        def s(context):
            return RunRequest(
                run_key=None,
                run_config=dagster_job_conf,
            )

        return s

    else:

        return None


def build_job_failure_sensor(job, lookback_minutes, max_retries):

    @sensor(
        description="Handle job failure",
        jobs=[job],
    )
    def s(context):

        events = context.instance.get_event_records(
            EventRecordsFilter(
                event_type=DagsterEventType.RUN_FAILURE,
                after_timestamp=(datetime.now() - timedelta(minutes=lookback_minutes)).timestamp()
            ),
            ascending=False,
            limit=max_retries,
        )
        processed = []
        for event in events:
            job_name = event.event_log_entry.job_name
            if job_name != job:
                continue

            run = context.instance.get_run_by_id(event.event_log_entry.run_id)
            if run.run_id in processed:
                continue

            attempt = int(run.tags.get("retry_attempt", 0))

            if attempt < max_retries:

                tags = run.tags
                tags["retry_attempt"] = str(attempts + 1)

                yield RunRequest(
                    run_key=run_info.run_id,
                    job_name=job_name,
                    run_config=run.run_config,
                    tags=tags,
                )

            processed.append(run.run_id)

    return s


def parse_yaml(content):
    start = 0
    start = content.find('{', start)
    end = content.find('}', start + 1)
    sub = content[start + 1 : end]
    if sub.startswith("timestamp"):
        if sub.find(':') > 8:
            format = sub[sub.find(':')+1:]
            # timestamp:%Y%m%d%H%M%S
            sub = datetime.datetime.now().strftime(format)
        else:
            sub = int(time.time())
    else:
        package_name = sub[:sub.rindex('.')]

        _locals = locals()
        exec(f"import {package_name}; sub={sub}", globals(), _locals)

    content = content[0:start] + str(_locals["sub"]) + content[end+1:]

    return content


@repository
def repo():

    conf_path = os.path.join(os.getcwd(), "conf", "local.yaml")
    with open(conf_path) as f:
        content = f.read()
        content = parse_yaml(content)
        job_conf = yaml.safe_load(content)

    dagster_job_conf, j, failure_sensor = build_job(job_conf)

    r = [j, failure_sensor]

    s = build_schedule(job_conf, dagster_job_conf, j)
    if s:
        r.append(s)

    return r


def main() -> int:
    r = repo()
    j = r[0]
    j.execute_in_process()
    
    return 0 if len(repo()) > 0 else 1


if __name__ == '__main__':

    sys.exit(main())
