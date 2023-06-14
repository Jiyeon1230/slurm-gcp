#!/usr/bin/env python3

# Copyright (C) SchedMD LLC.
# Copyright 2015 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import json
import logging
import os
import sys
from itertools import chain
from pathlib import Path
from suspend import delete_placement_groups
import yaml
import collections

import util
from util import (
    ensure_execute,
    get_insert_operations,
    log_api_request,
    map_with_futures,
    run,
    chunked,
    separate,
    to_hostlist,
    trim_self_link,
    wait_for_operation,
)
from util import cfg, lkp, NSDict

filename = Path(__file__).name
LOGFILE = (Path(cfg.slurm_log_dir if cfg else ".") / filename).with_suffix(".log")

log = logging.getLogger(filename)

global_resume_data = None

PLACEMENT_MAX_CNT = 150
# Placement group needs to be the same for an entire bulk_insert hence
# if placement is used the actual BULK_INSERT_LIMIT will be
# max([1000, PLACEMENT_MAX_CNT])
BULK_INSERT_LIMIT = 1000


def instance_properties(partition, model, placement_group, labels=None):
    node_group = lkp.node_group(model)
    template = lkp.node_template(model)
    template_info = lkp.template_info(template)

    props = NSDict()

    props.networkInterfaces = [
        {
            "subnetwork": partition.subnetwork,
        }
    ]

    if node_group.enable_public_ip:
        props.networkInterfaces[0]["accessConfigs"] = [
            {
                "type": "ONE_TO_ONE_NAT",
                "name": "External NAT",
                "networkTier": node_group.get("network_tier", None),
            }
        ]

    if node_group.bandwidth_tier == "virtio_enabled":
        props.networkInterfaces[0]["nicType"] = "VirtioNet"
    elif node_group.bandwidth_tier in ["tier_1_enabled", "gvnic_enabled"]:
        props.networkInterfaces[0]["nicType"] = "gVNIC"

    if node_group.bandwidth_tier == "tier_1_enabled":
        props.networkPerformanceConfig = {"totalEgressBandwidthTier": "TIER_1"}

    slurm_metadata = {
        "slurm_cluster_name": cfg.slurm_cluster_name,
        "slurm_instance_role": "compute",
        "startup-script": (
            Path(cfg.slurm_scripts_dir or util.dirs.scripts) / "startup.sh"
        ).read_text(),
        "VmDnsSetting": "GlobalOnly",
    }
    info_metadata = {
        item.get("key"): item.get("value") for item in template_info.metadata["items"]
    }

    props_metadata = {**info_metadata, **slurm_metadata}
    props.metadata = {
        "items": [NSDict({"key": k, "value": v}) for k, v in props_metadata.items()]
    }

    labels = {
        "slurm_cluster_name": cfg.slurm_cluster_name,
        "slurm_instance_role": "compute",
        **(labels or {}),
    }
    props.labels = {**template_info.labels, **labels}

    for disk in template_info.disks:
        disk.initializeParams.labels.update(labels)
    props.disks = template_info.disks

    if placement_group:
        props.scheduling = {
            "onHostMaintenance": "TERMINATE",
            "automaticRestart": False,
        }
        props.resourcePolicies = [
            placement_group,
        ]

    # provisioningModel=SPOT not supported by perInstanceProperties?
    if node_group.enable_spot_vm:
        util.compute = util.compute_service(version="beta")

        props.scheduling = {
            "automaticRestart": False,
            "instanceTerminationAction": node_group.spot_instance_config.get(
                "termination_action", "STOP"
            ),
            "onHostMaintenance": "TERMINATE",
            "preemptible": True,
            "provisioningModel": "SPOT",
        }

    return props


def per_instance_properties(node):
    props = NSDict()
    # No properties beyond name are supported yet.

    return props


def create_instances_request(nodes, placement_group, job_id=None):
    """Call regionInstances.bulkInsert to create instances"""
    assert len(nodes) > 0
    if placement_group:
        assert len(nodes) <= min(PLACEMENT_MAX_CNT, BULK_INSERT_LIMIT)
    else:
        assert len(nodes) <= BULK_INSERT_LIMIT

    # model here indicates any node that can be used to describe the rest
    model = next(iter(nodes))
    partition = lkp.node_partition(model)
    template = lkp.node_template(model)
    region = lkp.node_region(model)

    body = NSDict()
    body.count = len(nodes)
    body.minCount = 1

    # source of instance properties
    body.sourceInstanceTemplate = template

    labels = (
        dict(slurm_job_id=job_id)
        if job_id is not None and partition.enable_job_exclusive
        else None
    )
    # overwrites properties accross all instances
    body.instanceProperties = instance_properties(
        partition, model, placement_group, labels
    )

    # key is instance name, value overwrites properties
    body.perInstanceProperties = {k: per_instance_properties(k) for k in nodes}

    zones = {
        **{
            f"zones/{zone}": {"preference": "ALLOW"}
            for zone in partition.zone_policy_allow or []
        },
        **{
            f"zones/{zone}": {"preference": "DENY"}
            for zone in partition.zone_policy_deny or []
        },
    }
    body.locationPolicy.targetShape = cfg.zone_target_shape or "ANY_SINGLE_ZONE"
    if zones:
        body.locationPolicy.locations = zones

    request = util.compute.regionInstances().bulkInsert(
        project=cfg.project, region=region, body=body.to_dict()
    )

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            f"new request: endpoint={request.methodId} nodes={to_hostlist(nodes)}"
        )
    log_api_request(request)
    return request


def expand_nodelist(nodelist):
    """expand nodes in hostlist to hostnames"""
    if not nodelist:
        return []

    # TODO use a python library instead?
    nodes = run(f"{lkp.scontrol} show hostnames {nodelist}").stdout.splitlines()
    return nodes


def group_nodes_bulk(nodes, resume_data=None):
    """group nodes by job_id, placement_group, node_group, and max bulkInsert size"""

    if resume_data is None and global_resume_data is not None:
        resume_data = global_resume_data.deepcopy()
    if resume_data is None:
        # all nodes will be considered jobless
        jobs = {}
    else:
        jobs = {job.job_id: job for job in resume_data.jobs}

    # expand all job nodelists
    for job in jobs.values():
        job.nodelist_alloc = job.nodes_alloc
        job.nodes_alloc = expand_nodelist(job.nodelist_alloc)
        job.nodelist_resume = job.nodes_resume
        job.nodes_resume = expand_nodelist(job.nodelist_resume)
        # create placement groups if nodes for job need it
        job.placement_groups = create_placement_groups(job.job_id, job.nodes_alloc)
        # placement group assignment is based on all allocated nodes, but we only want to
        # handle nodes in nodes_resume in this run.
        for pg, pg_nodes in job.placement_groups.items():
            job.placement_groups[pg] = list(
                set(pg_nodes).intersection(job.nodes_resume)
            )
    # a bit of a hack, but nodes resumed using scontrol instead of through job scheduling do not have a job
    jobless_nodes = list(
        set(nodes).difference(
            chain.from_iterable(job.nodes_resume for job in jobs.values())
        )
    )
    jobs[None] = NSDict(
        job_id=None,
        nodes_resume=jobless_nodes,
        nodes_alloc=jobless_nodes,
        placement_groups={None: jobless_nodes},
    )

    BulkChunk = collections.namedtuple(
        "BulkChunk", ["prefix", "job_id", "placement_group", "nodes", "i"]
    )
    grouped_nodes = [
        BulkChunk(prefix, job_id, placement_group, chunk_nodes, i)
        for job_id, job in jobs.items()
        for placement_group, pg_nodes in job.placement_groups.items()
        for prefix, nodes in util.groupby_unsorted(pg_nodes, lkp.node_prefix)
        for i, chunk_nodes in enumerate(chunked(nodes, n=BULK_INSERT_LIMIT))
    ]

    def group_name(chunk: BulkChunk):
        if chunk.placement_group is not None:
            return f"{chunk.prefix}:job{chunk.job_id}:{chunk.placement_group}:{chunk.i}"
        if chunk.job_id is not None:
            return f"{chunk.prefix}:job{chunk.job_id}:{chunk.i}"
        return f"{chunk.prefix}:{chunk.i}"

    grouped_nodes = {group_name(chunk): chunk for chunk in grouped_nodes}
    return grouped_nodes


def resume_nodes(nodes, resume_data=None):
    """resume nodes in nodelist"""
    # support already expanded list
    if isinstance(nodes, str):
        nodelist = nodes
        nodes = expand_nodelist(nodelist)
    if len(nodes) == 0:
        log.info("No nodes to resume")
        return
    nodes = sorted(nodes, key=lkp.node_prefix)

    grouped_nodes = group_nodes_bulk(nodes, resume_data)

    if log.isEnabledFor(logging.DEBUG):
        # grouped_nodelists is used in later debug logs too
        grouped_nodelists = {
            group: to_hostlist(chunk.nodes) for group, chunk in grouped_nodes.items()
        }
        log.debug(
            "node bulk groups: \n{}".format(yaml.safe_dump(grouped_nodelists).rstrip())
        )

    # make all bulkInsert requests and execute with batch
    inserts = {
        group: create_instances_request(
            chunk.nodes, chunk.placement_group, chunk.job_id
        )
        for group, chunk in grouped_nodes.items()
    }

    bulk_ops = dict(
        zip(inserts.keys(), map_with_futures(ensure_execute, inserts.values()))
    )
    log.debug(f"bulk_ops={yaml.safe_dump(bulk_ops)}")
    started = {
        group: op for group, op in bulk_ops.items() if not isinstance(op, Exception)
    }
    failed = {
        group: err for group, err in bulk_ops.items() if isinstance(err, Exception)
    }
    if failed:
        failed_reqs = [str(e) for e in failed.items()]
        log.error("bulkInsert API failures: {}".format("; ".join(failed_reqs)))
        for ident, exc in failed.items():
            down_nodes(grouped_nodes[ident].nodes, exc._get_reason())

    if log.isEnabledFor(logging.DEBUG):
        for group, op in started.items():
            group_nodes = grouped_nodelists[group]
            name = op["name"]
            gid = op["operationGroupId"]
            log.debug(
                f"new bulkInsert operation started: group={group} nodes={group_nodes} name={name} operationGroupId={gid}"
            )
    # wait for all bulkInserts to complete and log any errors
    bulk_operations = {group: wait_for_operation(op) for group, op in started.items()}
    all_successful_inserts = []

    for group, bulk_op in bulk_operations.items():
        group_id = bulk_op["operationGroupId"]
        bulk_op_name = bulk_op["name"]
        if "error" in bulk_op:
            error = bulk_op["error"]["errors"][0]
            group_nodes = to_hostlist(grouped_nodes[group].nodes)
            log.warning(
                f"bulkInsert operation errors: {error['code']} name={bulk_op_name} operationGroupId={group_id} nodes={group_nodes}"
            )
        successful_inserts, failed_inserts = separate(
            lambda op: "error" in op, get_insert_operations(group_id)
        )
        # Apparently multiple errors are possible... so join with +.
        by_error_inserts = util.groupby_unsorted(
            failed_inserts,
            lambda op: "+".join(err["code"] for err in op["error"]["errors"]),
        )
        for code, failed_ops in by_error_inserts:
            failed_nodes = {trim_self_link(op["targetLink"]): op for op in failed_ops}
            hostlist = util.to_hostlist(failed_nodes)
            count = len(failed_nodes)
            log.error(
                f"{count} instances failed to start: {code} ({hostlist}) operationGroupId={group_id}"
            )
            failed_node, failed_op = next(iter(failed_nodes.items()))
            msg = "; ".join(
                f"{err['code']}: {err['message'] if 'message' in err else 'no message'}"
                for err in failed_op["error"]["errors"]
            )
            if code != "RESOURCE_ALREADY_EXISTS":
                down_nodes(hostlist, msg)
            log.error(
                f"errors from insert for node '{failed_node}' ({failed_op['name']}): {msg}"
            )

        ready_nodes = {trim_self_link(op["targetLink"]) for op in successful_inserts}
        if len(ready_nodes) > 0:
            ready_nodelist = to_hostlist(ready_nodes)
            log.info(f"created {len(ready_nodes)} instances: nodes={ready_nodelist}")
            all_successful_inserts.extend(successful_inserts)


def update_job_comment(nodelist, comment):
    resume_data = global_resume_data
    if resume_data is None:
        log.error("Cannot update and notify jobs with API failures.")
        return
    if isinstance(nodelist, list):
        nodelist = util.to_hostlist(nodelist)

    job_list = (
        job
        for job in resume_data.jobs
        if any(map(lambda each: each in nodelist, to_hostlist(job.nodes)))
    )
    for job in job_list:
        run(f"{lkp.scontrol} update jobid={job.job_id} admincomment='{comment}'")
        run(f"{lkp.scontrol} notify {job.job_id} '{comment}'")


def down_nodes(nodelist, reason):
    """set nodes down with reason"""
    if isinstance(nodelist, list):
        nodelist = util.to_hostlist(nodelist)
    update_job_comment(nodelist, reason)
    run(f"{lkp.scontrol} update nodename={nodelist} state=down reason='{reason}'")


def hold_job(job_id, reason):
    """hold job, set comment to reason"""
    run(f"{lkp.scontrol} hold jobid={job_id}")
    run(f"{lkp.scontrol} update jobid={job_id} comment='{reason}'")


def create_placement_request(pg_name, region):
    config = {
        "name": pg_name,
        "region": region,
        "groupPlacementPolicy": {
            "collocation": "COLLOCATED",
        },
    }
    request = util.compute.resourcePolicies().insert(
        project=cfg.project, region=region, body=config
    )
    log_api_request(request)
    return request


def create_placement_groups(job_id, node_list):
    model = next(iter(node_list))
    partition = lkp.node_partition(model)
    # allow calling this method on non-placement group nodes
    if not partition.enable_placement_groups:
        return {None: node_list}
    partition_name = partition.partition_name
    region = lkp.node_region(model)

    groups = {
        f"{cfg.slurm_cluster_name}-{partition_name}-{job_id}-{i}": nodes
        for i, nodes in enumerate(chunked(node_list, n=PLACEMENT_MAX_CNT))
    }

    if log.isEnabledFor(logging.DEBUG):
        debug_groups = {group: to_hostlist(nodes) for group, nodes in groups.items()}
        log.debug(
            f"creating {len(groups)} placement groups: \n{yaml.safe_dump(debug_groups).rstrip()}"
        )
    requests = {
        group: create_placement_request(group, region)
        for group, incl_nodes in groups.items()
    }
    ops = dict(
        zip(requests.keys(), map_with_futures(ensure_execute, requests.values()))
    )

    def classify_result(item):
        op = item[1]
        if not isinstance(op, Exception):
            return "submitted"
        if all(e.get("reason") == "alreadyExists" for e in op.error_details):
            return "redundant"
        return "failed"

    grouped_ops = dict(util.groupby_unsorted(list(ops.items()), classify_result))
    submitted, redundant, failed = (
        dict(grouped_ops.get(key, {})) for key in ("submitted", "redundant", "failed")
    )
    if redundant:
        log.warning(
            "placement policies already exist: {}".format(",".join(redundant.keys()))
        )
    any_failures = False
    if failed:
        reqs = [f"{e}" for _, e in failed.values()]
        log.fatal("failed to create placement policies: {}".format("; ".join(reqs)))
        any_failures = True
    operations = {group: wait_for_operation(op) for group, op in submitted.items()}
    for group, op in operations.items():
        if "error" in op:
            msg = "; ".join(
                f"{err['code']}: {err['message'] if 'message' in err else 'no message'}"
                for err in op["error"]["errors"]
            )
            log.error(
                f"placement group failed to create: '{group}' ({op['name']}): {msg}"
            )
            any_failures = True

    if any_failures:
        # delete any placement groups that managed to be created.
        delete_placement_groups(job_id, region, partition_name)
        exit(1)
    log.info(
        f"created {len(operations)} placement groups ({to_hostlist(operations.keys())})"
    )
    return groups


def valid_placement_nodes(job_id, nodelist):
    machine_types = {
        lkp.node_prefix(node): lkp.node_template_info(node).machineType
        for node in nodelist
    }
    fail = False
    valid_types = ["a2", "c2", "c2d", "c3", "n2", "n2d"]
    for prefix, machine_type in machine_types.items():
        if machine_type.split("-")[0] not in valid_types:
            log.warn(f"Unsupported machine type for placement policy: {machine_type}.")
            fail = True
    if fail:
        log.warn(
            f"Please use a valid machine type with placement policy: ({','.join(valid_types)})"
        )
        return False
    return True


def get_resume_file_data():
    SLURM_RESUME_FILE = os.getenv("SLURM_RESUME_FILE")
    if SLURM_RESUME_FILE is None:
        log.warning(
            "SLURM_RESUME_FILE was not in environment. Cannot get detailed job, node, partition allocation data."
        )
        return None
    resume_file = Path(SLURM_RESUME_FILE)
    return NSDict(json.loads(resume_file.read_text()))


def main(nodelist, force=False):
    """main called when run as script"""
    log.debug(f"ResumeProgram {nodelist}")
    nodes = expand_nodelist(nodelist)

    # Filter out nodes not in config.yaml
    cloud_nodes, local_nodes = lkp.filter_nodes(nodes)
    if len(local_nodes) > 0:
        log.debug(
            f"Ignoring slurm-gcp external nodes '{util.to_hostlist(local_nodes)}' from '{nodelist}'"
        )
    cloud_nodelist = util.to_hostlist(cloud_nodes)
    if len(cloud_nodes) > 0:
        log.debug(f"Using cloud nodes '{cloud_nodelist}' from '{nodelist}'")
    else:
        log.debug("No cloud nodes to resume")
        return

    log.info(f"resume {cloud_nodelist}")
    resume_nodes(cloud_nodes, global_resume_data)


parser = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
)
parser.add_argument("nodelist", help="list of nodes to resume")
parser.add_argument(
    "--force",
    "-f",
    "--static",
    action="store_true",
    help="Force attempted creation of the nodelist, whether nodes are exclusive or not.",
)
parser.add_argument(
    "--debug",
    "-d",
    dest="loglevel",
    action="store_const",
    const=logging.DEBUG,
    default=logging.INFO,
    help="Enable debugging output",
)
parser.add_argument(
    "--trace-api",
    "-t",
    action="store_true",
    help="Enable detailed api request output",
)


if __name__ == "__main__":
    args = parser.parse_args()

    if cfg.enable_debug_logging:
        args.loglevel = logging.DEBUG
    if args.trace_api:
        cfg.extra_logging_flags = list(cfg.extra_logging_flags)
        cfg.extra_logging_flags.append("trace_api")
    util.chown_slurm(LOGFILE, mode=0o600)
    util.config_root_logger(filename, level=args.loglevel, logfile=LOGFILE)
    sys.excepthook = util.handle_exception

    global_resume_data = NSDict(get_resume_file_data())
    main(args.nodelist, args.force)
