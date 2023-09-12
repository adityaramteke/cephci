"""
core_workflows module is a rados layer configuration module for Ceph cluster.
It allows us to perform various day1 and day2 operations such as
1. Creating , modifying, setting , getting, writing, scrubbing, reading various pools like EC and replicated
2. Increase decrease PG counts, enable - disable - configure modules that do this
3. Enable logging to file, set and reset config params and cluster checks
4. Set-up email alerts and other cluster operations
More operations to be added as needed

"""

import datetime
import json
import re
import time

from ceph.ceph_admin import CephAdmin
from ceph.parallel import parallel
from utility.log import Log

log = Log(__name__)


class RadosOrchestrator:
    """
    RadosOrchestrator class contains various methods that perform various day1 and day2 operations on the cluster
    Usage: The class is initialized with the CephAdmin object for various operations
    """

    def __init__(self, node: CephAdmin):
        """
        initializes the env to run rados commands
        Args:
            node: CephAdmin object
        """
        self.node = node
        self.ceph_cluster = node.cluster
        self.client = node.cluster.get_nodes(role="client")[0]
        self.rhbuild = node.config.get("rhbuild")

    def change_recovery_flags(self, action):
        """Sets and unsets the recovery flags on the cluster

        This method is used to control the recovery and backfill aspects of the cluster by setting/ un-setting
        the below flags on the cluster at global level.
        |nobackfill|norebalance|norecover|

        Args:
            action (str): "set" & "unset" are the allowed actions for the method

        Examples::
            change_recovery_flags(action="set")

        Returns: None
        """
        flags = ["nobackfill", "norebalance", "norecover"]
        log.debug(
            f"{action}-ing recovery flags on the cluster to change recovery behaviour"
        )
        for flag in flags:
            cmd = f"ceph osd {action} {flag}"
            self.node.shell([cmd])

    def check_pg_state(self, pgid: str) -> list:
        """Fetches and returns the state of PG given

        Args:
            pgid (str): PG ID

        Examples:
            check_pg_state(pgid=11.2f)

        Returns: list of PG states for the PG
        """
        log.debug(f"Checking the PG state for PG ID : {pgid} ")
        cmd = "ceph pg dump pgs"
        pg_stats = self.run_ceph_command(cmd)
        for pg in pg_stats["pg_stats"]:
            if pg["pgid"] == pgid:
                return pg["state"]
        log.error(f"could not find the given pg : {pgid}")
        return []

    def enable_email_alerts(self, **kwargs) -> bool:
        """
        Enables the email alerts module and configures alerts to be sent
        References : https://docs.ceph.com/en/latest/mgr/alerts/
        Args:
            **kwargs: Any other param that needs to be set
            Various args that can be passed are :
            1. smtp_host
            2. smtp_sender
            3. smtp_ssl
            4. smtp_port
            5. interval
            6. smtp_from_name
            7. smtp_destination
        Returns: True -> pass, False -> fail
        """
        alert_cmds = {
            "smtp_host": f"ceph config set mgr mgr/alerts/smtp_host "
            f"{kwargs.get('smtp_host', 'smtp.corp.redhat.com')}",
            "smtp_sender": f"ceph config set mgr mgr/alerts/smtp_sender "
            f"{kwargs.get('smtp_sender', 'ceph-iad2-c01-lab.mgr@redhat.com')}",
            "smtp_ssl": f"ceph config set mgr mgr/alerts/smtp_ssl {kwargs.get('smtp_ssl', 'false')}",
            "smtp_port": f"ceph config set mgr mgr/alerts/smtp_port {kwargs.get('smtp_port', '25')}",
            "interval": f"ceph config set mgr mgr/alerts/interval {kwargs.get('interval', '5')}",
            "smtp_from_name": f"ceph config set mgr mgr/alerts/smtp_from_name "
            f"'{kwargs.get('smtp_from_name', 'Rados 5.0 sanity Cluster')}'",
        }
        cmd = "ceph mgr module enable alerts"
        self.node.shell([cmd])

        for cmd in alert_cmds.values():
            self.node.shell([cmd])

        if kwargs.get("smtp_destination"):
            for email in kwargs.get("smtp_destination"):
                cmd = f"ceph config set mgr mgr/alerts/smtp_destination {email}"
                self.node.shell([cmd])
        else:
            log.error("email addresses not provided")
            return False

        # Printing all the configuration set
        cmd = "ceph config dump"
        log.info(self.run_ceph_command(cmd))

        # Disabling and enabling the email alert module after setting all the config
        states = ["disable", "enable"]
        for state in states:
            cmd = f"ceph mgr module {state} alerts"
            self.node.shell([cmd])
            time.sleep(1)

        # Triggering email alert
        try:
            cmd = "ceph alerts send"
            self.node.shell([cmd])
        except Exception:
            log.error("Error while Sending email alerts")
            return False

        log.info("Email alerts configured on the cluster")
        return True

    def run_ceph_command(self, cmd: str, timeout: int = 300, client_exec: bool = False):
        """
        Runs ceph commands with json tag for the action specified otherwise treats action as command
        and returns formatted output
        Args:
            cmd: Command that needs to be run
            timeout: Maximum time allowed for execution.
            client_exec: Selection if true, runs the command on the client node
        Returns: dictionary of the output
        """

        cmd = f"{cmd} -f json"
        try:
            if client_exec:
                out, err = self.client.exec_command(cmd=cmd, sudo=True, timeout=timeout)
            else:
                out, err = self.node.shell([cmd], timeout=timeout)
        except Exception as er:
            log.error(f"Exception hit while command execution. {er}")
            return None
        if out.isspace():
            return {}
        status = json.loads(out)
        return status

    def pool_inline_compression(self, pool_name: str, **kwargs) -> bool:
        """
        BlueStore supports inline compression using snappy, zlib, or lz4.
        This module sets various compression modes and other related configs
        Args:
            pool_name: pool name on which compression needs to be enabled and configured
            **kwargs: Various args that can be passed:
                1. compression_mode : Whether data in BlueStore is compressed is determined by  compression mode.
                    The modes are:
                        none: Never compress data.
                        passive: Do not compress data unless the write operation has a compressible hint set.
                        aggressive: Compress data unless the write operation has an incompressible hint set.
                        force: Try to compress data no matter what.
                2. compression_algorithm : compression algorithm to be used.
                    Supported:
                        <empty string>
                        snappy
                        zlib
                        zstd
                        lz4
                3. compression_required_ratio : The ratio of the size of the data chunk after compression.
                    eg : 0.7
                4. compression_min_blob_size : Chunks smaller than this are never compressed.
                    eg : 10B
                5. compression_max_blob_size : Chunks larger than this value are broken into smaller blobs
                    eg : 10G
        Returns: Pass -> true , Fail -> false
        """

        if pool_name not in self.list_pools():
            log.error(f"requested pool {pool_name} is not present on the cluster")
            return False

        value_map = {
            "compression_algorithm": kwargs.get("compression_algorithm", "snappy"),
            "compression_mode": kwargs.get("compression_mode", "none"),
            "compression_required_ratio": kwargs.get(
                "compression_required_ratio", 0.875
            ),
            "compression_min_blob_size": kwargs.get("compression_min_blob_size", "0B"),
            "compression_max_blob_size": kwargs.get("compression_max_blob_size", "0B"),
        }

        # Adding the config values
        for val in value_map.keys():
            if kwargs.get(val, False):
                cmd = f"ceph osd pool set {pool_name} {val} {value_map[val]}"
                self.node.shell([cmd])

        details = self.run_ceph_command(cmd="ceph osd dump")
        for detail in details["pools"]:
            if detail["pool_name"] == pool_name:
                compression_conf = detail["options"]
                if (
                    not compression_conf["compression_algorithm"]
                    == value_map["compression_algorithm"]
                ):
                    log.error("Compression algorithm not set")
                    return False
        # tbd: Verify if compression set is working as expected. Compression ratio to be maintained
        log.info(f"compression set on pool {pool_name} successfully")
        return True

    def list_pools(self) -> list:
        """
        Collect the list of pools present on the cluster
        Returns: list of pool names
        """
        cmd = "ceph df"
        out = self.run_ceph_command(cmd=cmd)
        return [entry["name"] for entry in out["pools"]]

    def get_pool_property(self, pool, props):
        """
        Used to fetch a given property set on the pool
        Args:
            pool: name of the pool
            props: property to be fetched.
            Allowed values :
            size|min_size|pg_num|pgp_num|crush_rule|hashpspool|nodelete|nopgchange|nosizechange|
            write_fadvise_dontneed|noscrub|nodeep-scrub|hit_set_type|hit_set_period|hit_set_count|
            hit_set_fpp|use_gmt_hitset|target_max_objects|target_max_bytes|cache_target_dirty_ratio|
            cache_target_dirty_high_ratio|cache_target_full_ratio|cache_min_flush_age|cache_min_evict_age|
            erasure_code_profile|min_read_recency_for_promote|all|min_write_recency_for_promote|fast_read|
            hit_set_grade_decay_rate|hit_set_search_last_n|scrub_min_interval|scrub_max_interval|
            deep_scrub_interval|recovery_priority|recovery_op_priority|scrub_priority|compression_mode|
            compression_algorithm|compression_required_ratio|compression_max_blob_size|
            compression_min_blob_size|csum_type|csum_min_block|csum_max_block|allow_ec_overwrites|
            fingerprint_algorithm|pg_autoscale_mode|pg_autoscale_bias|pg_num_min|target_size_bytes|
            target_size_ratio|dedup_tier|dedup_chunk_algorithm|dedup_cdc_chunk_size
        Returns: key value pair for the requested property
        Note : Trying to fetch the value for property, which has not been set will error out
        """
        # checking if the pool exists
        if pool not in self.list_pools():
            log.error(f"requested pool {pool} is not present on the cluster")
            return False

        cmd = f"ceph osd pool get {pool} {props} -f json"
        out, err = self.node.shell([cmd])
        prop_details = json.loads(out)
        return prop_details

    def get_pool_details(self, pool) -> dict:
        """
        Method to fetch the properties of the pool via ceph osd pool ls commands

        Args:
            pool: name of the pool
        returns:
            Dictionary of pool properties for the selected pool
        """
        cmd = "ceph osd pool ls detail"
        out = self.run_ceph_command(cmd=cmd)
        for ele in out:
            if ele["pool_name"] == pool:
                return ele
        log.error(f"pool {pool} not found")
        return {}

    def host_maintenance_enter(self, hostname: str, retry: int = 10) -> bool:
        """
        Adds the specified host into maintenance mode
        Args:
            hostname: name of the host which needs to be added into maintenance mode
            retry: max number of retries to put host into maintenance mode
        Returns:
            True -> Host successfully added to maintenance mode
            False -> Host Could not be added to maintenance mode
        """
        log.debug(f"Passed host : {hostname} to be added into maintenance mode")
        iteration = 0

        while iteration <= retry:
            iteration += 1
            cmd = f"ceph orch host maintenance enter {hostname} --force"
            out, _ = self.client.exec_command(cmd=cmd, sudo=True, timeout=600)
            log.debug(f"o/p of maintenance enter cmd : {out}")
            time.sleep(20)

            if not self.check_host_status(hostname=hostname, status="maintenance"):
                log.error(
                    f"Host: {hostname}, not in maintenance mode. Retrying again in 15 seconds, Retry count :{iteration}"
                )
                # retrying in 15 seconds
                time.sleep(15)
                if iteration == retry:
                    return False
            else:
                log.info(f"Added host {hostname} into maintenance mode on the cluster")
                return True

    def host_maintenance_exit(self, hostname: str, retry: int = 3) -> bool:
        """
        Removes the specified host from maintenance mode
        Args:
            hostname: name of the host which needs to be removed from maintenance mode
            retry: no of retries to be done to remove host from maintenance mode

        Returns:
            True -> Host successfully added to maintenance mode
            False -> Host Could not be added to maintenance mode
        """
        log.debug(f"Passed host : {hostname} to be removed from maintenance mode")
        iteration = 0

        while iteration <= retry:
            iteration += 1
            cmd = f"ceph orch host maintenance exit {hostname} "
            out, _ = self.client.exec_command(cmd=cmd, sudo=True, timeout=600)
            log.debug(f"o/p of maintenance exit cmd : {out}")
            time.sleep(20)

            if not self.check_host_status(hostname=hostname):
                log.error(
                    f"Host:{hostname}, in maintenance mode. Retrying again in 15 seconds, Retry count :{iteration}"
                )
                # retrying in 15 seconds
                time.sleep(15)
                if iteration == retry:
                    return False
            else:
                log.info(
                    f"Removed host {hostname} from maintenance mode on the cluster"
                )
                return True

    def set_pool_property(self, pool, props, value):
        """
        Used to fetch a given property set on the pool
        Args:
            pool: name of the pool
            props: property to be set on pool.
                Allowed values :
                size|min_size|pg_num|pgp_num|crush_rule|hashpspool|nodelete|nopgchange|nosizechange|
                write_fadvise_dontneed|noscrub|nodeep-scrub|hit_set_type|hit_set_period|hit_set_count|
                hit_set_fpp|use_gmt_hitset|target_max_objects|target_max_bytes|cache_target_dirty_ratio|
                cache_target_dirty_high_ratio|cache_target_full_ratio|cache_min_flush_age|cache_min_evict_age|
                erasure_code_profile|min_read_recency_for_promote|all|min_write_recency_for_promote|fast_read|
                hit_set_grade_decay_rate|hit_set_search_last_n|scrub_min_interval|scrub_max_interval|
                deep_scrub_interval|recovery_priority|recovery_op_priority|scrub_priority|compression_mode|
                compression_algorithm|compression_required_ratio|compression_max_blob_size|
                compression_min_blob_size|csum_type|csum_min_block|csum_max_block|allow_ec_overwrites|
                fingerprint_algorithm|pg_autoscale_mode|pg_autoscale_bias|pg_num_min|target_size_bytes|
                target_size_ratio|dedup_tier|dedup_chunk_algorithm|dedup_cdc_chunk_size
            value: value to be set for the property
        Returns: Pass -> True, Fail -> False
        """
        # checking if the pool exists
        if pool not in self.list_pools():
            log.error(f"requested pool {pool} is not present on the cluster")
            return False

        cmd = f"ceph osd pool set {pool} {props} {value}"
        out, err = self.node.shell([cmd])
        # sleeping for 2 seconds for the values to reflect
        time.sleep(2)
        log.info(f"property {props} set on pool {pool}")
        return True

    def bench_write(self, pool_name: str, **kwargs) -> bool:
        """
        Method to trigger Write operations via the Rados Bench tool
        Args:
            pool_name: pool on which the operation will be performed
            kwargs: Any other param that needs to passed
            1. rados_write_duration -> duration of write operation (int)
            2. byte_size -> size of objects to be written (str)
                eg : 10KB, 4096
            3. max_objs -> max number of objects to be written (int)
            4. verify_stats -> arg to control whether obj stats need to
            be verified after write (bool) | default: True
        Returns: True -> pass, False -> fail
        """
        duration = kwargs.get("rados_write_duration", 200)
        byte_size = kwargs.get("byte_size", 4096)
        max_objs = kwargs.get("max_objs")
        verify_stats = kwargs.get("verify_stats", True)
        cmd = f"sudo rados --no-log-to-stderr -b {byte_size} -p {pool_name} bench {duration} write --no-cleanup"
        org_objs = self.get_cephdf_stats(pool_name=pool_name)["stats"]["objects"]
        if max_objs:
            cmd = f"{cmd} --max-objects {max_objs}"

        try:
            self.node.shell([cmd])
            if max_objs and verify_stats:
                time.sleep(90)
                new_objs = self.get_cephdf_stats(pool_name=pool_name)["stats"][
                    "objects"
                ]
                log.info(
                    f"Objs in the {pool_name} before IOPS: {org_objs} "
                    f"| Objs in the pool post IOPS: {new_objs} "
                    f"| Expected {org_objs + max_objs} or {org_objs + max_objs + 1}"
                )
                assert (new_objs == org_objs + max_objs) or (
                    new_objs == org_objs + max_objs + 1
                )
            else:
                time.sleep(15)
                new_objs = self.get_cephdf_stats(pool_name=pool_name)["stats"][
                    "objects"
                ]
                log.info(
                    f"Objs in the {pool_name} before IOPS: {org_objs} "
                    f"| Objs in the pool post IOPS: {new_objs} "
                    f"| Expected {new_objs} > 0"
                )
                assert new_objs > 0
            return True
        except Exception as err:
            log.error(f"Error running rados bench write on pool : {pool_name}")
            log.error(err)
            return False

    def bench_read(self, pool_name: str, **kwargs) -> bool:
        """
        Method to trigger Read operations via the Rados Bench tool
        Args:
            pool_name: pool on which the operation will be performed
            kwargs: Any other param that needs to passed
                1. rados_read_duration -> duration of read operation (int)
        Returns: True -> pass, False -> fail
        """
        duration = kwargs.get("rados_read_duration", 80)
        try:
            cmd = f"rados --no-log-to-stderr -p {pool_name} bench {duration} seq"
            self.node.shell([cmd])
            cmd = f"rados --no-log-to-stderr -p {pool_name} bench {duration} rand"
            self.node.shell([cmd])
            return True
        except Exception as err:
            log.error(f"Error running rados bench write on pool : {pool_name}")
            log.error(err)
            return False

    def create_pool(self, pool_name: str, **kwargs) -> bool:
        """
        Create a pool named from the pool_name parameter.
         Args:
            pool_name: name of the pool being created.
            kwargs: Any other args that need to be passed
                1. pg_num -> number of PG's and PGP's
                2. ec_profile_name -> name of EC profile if pool being created is an EC pool
                3. min_size -> min replication size for pool to serve data
                4. size -> min replication size for pool to write data
                5. erasure_code_use_overwrites -> allows overrides in an erasure coded pool
                6. allow_ec_overwrites -> This lets RBD and CephFS store their data in an erasure coded pool
                7. disable_pg_autoscale -> sets auto-scale mode off on the pool
                8. crush_rule -> custom crush rule for the pool
                9. pool_quota -> limit the maximum number of objects or the maximum number of bytes stored
         Returns: True -> pass, False -> fail
        """

        log.debug(f"creating pool_name {pool_name}")
        cmd = f"ceph osd pool create {pool_name}"
        if kwargs.get("pg_num"):
            cmd = f"{cmd} {kwargs['pg_num']} {kwargs['pg_num']}"
        if kwargs.get("pg_num_max"):
            cmd = f"{cmd} --pg_num_max {kwargs['pg_num_max']}"
        if kwargs.get("ec_profile_name"):
            cmd = f"{cmd} erasure {kwargs['ec_profile_name']}"
        if kwargs.get("bulk"):
            cmd = f"{cmd} --bulk"
        try:
            self.node.shell([cmd])
        except Exception as err:
            log.error(f"Error creating pool : {pool_name}")
            log.error(err)
            return False

        # Enabling rados application on the pool
        enable_app_cmd = f"sudo ceph osd pool application enable {pool_name} {kwargs.get('app_name', 'rados')}"
        self.node.shell([enable_app_cmd])

        if kwargs.get("app_name") == "rbd":
            pool_init = f"rbd pool init -p {pool_name}"
            self.node.shell([pool_init])

        cmd_map = {
            "min_size": f"ceph osd pool set {pool_name} min_size {kwargs.get('min_size')}",
            "size": f"ceph osd pool set {pool_name} size {kwargs.get('size')}",
            "erasure_code_use_overwrites": f"ceph osd pool set {pool_name} "
            f"allow_ec_overwrites {kwargs.get('erasure_code_use_overwrites')}",
            "disable_pg_autoscale": f"ceph osd pool set {pool_name} pg_autoscale_mode off",
            "crush_rule": f"sudo ceph osd pool set {pool_name} crush_rule {kwargs.get('crush_rule')}",
            "pool_quota": f"ceph osd pool set-quota {pool_name} {kwargs.get('pool_quota')}",
        }
        for key in kwargs:
            if cmd_map.get(key):
                try:
                    self.node.shell([cmd_map[key]])
                except Exception as err:
                    log.error(
                        f"Error setting the property : {key} for pool : {pool_name}"
                    )
                    log.error(err)
                    return False
        time.sleep(5)
        log.info(f"Created pool {pool_name} successfully")
        return True

    def change_recovery_threads(self, config: dict, action: str):
        """
        increases or decreases the recovery threads based on the action sent
        Args:
            config: Config from the suite file for the run
            action: Set or remove increase the backfill / recovery threads
                Values : "set" -> set the threads to specified value
                         "rm" -> remove the config changes made
        """

        cfg_map = {
            "osd_max_backfills": f"ceph config {action} osd osd_max_backfills",
            "osd_recovery_max_active": f"ceph config {action} osd osd_recovery_max_active",
        }
        if self.check_osd_op_queue(qos="mclock"):
            self.node.shell(
                ["ceph config set osd osd_mclock_override_recovery_settings true"]
            )
        for cmd in cfg_map:
            if action == "set":
                command = f"{cfg_map[cmd]} {config.get(cmd, 8)}"
            else:
                command = cfg_map[cmd]
            self.node.shell([command])

    def get_pg_acting_set(self, **kwargs) -> list:
        """
        Fetches the PG details about the given pool and then returns the acting set of OSD's from sample PG of the pool
        Args:
            kwargs: Args that can be passed to fetch acting set
                pool_name: name of the pool whose one of the acting OSD set is needed.
                pg_num: pg whose acting set needs to be fetched
                None: Collects the acting set of pool with ID 1
            eg:
        Returns: list osd's part of acting set
        eg : [3,15,20]
        """
        if kwargs.get("pool_name"):
            pool_name = kwargs["pool_name"]
            # Collecting details about the cluster
            cmd = "ceph osd dump"
            out = self.run_ceph_command(cmd=cmd)
            for val in out["pools"]:
                if val["pool_name"] == pool_name:
                    pool_id = val["pool"]
                    break
            # Collecting the details of the 1st PG in the pool <ID>.0
            pg_num = f"{pool_id}.0"

        elif kwargs.get("pg_num"):
            pg_num = kwargs["pg_num"]

        else:
            # Collecting the acting set for a random pool ID 1 from cluster
            pg_num = "1.0"

        log.debug(f"Collecting the acting set for the PG : {pg_num}")
        cmd = f"ceph pg map {pg_num}"
        out = self.run_ceph_command(cmd=cmd)
        return out["up"]

    def run_scrub(self, **kwargs):
        """
        Run scrub on the given OSD or on all OSD's
         Args:
            kwargs:
            1. osd : if an OSD id is passed , scrub to be triggered on that osd
                    eg- obj.run_scrub(osd=3)
            2. pgid: if a PGID is passed, scrubs are run on that PG
                    eg- obj.run_scrub(pgid=1.0)
            3. pool: if pool name is passed, scrubs are run on that pool
                    eg- obj.run_scrub(pool="test-pool")
         Returns: None
        """
        if kwargs.get("osd"):
            cmd = f"ceph osd scrub {kwargs.get('osd')}"
        elif kwargs.get("pgid"):
            cmd = f"ceph pg scrub {kwargs.get('pgid')}"
        elif kwargs.get("pool"):
            cmd = f"ceph osd pool scrub {kwargs.get('pool')}"
        else:
            # scrubbing all the OSD's
            cmd = "ceph osd scrub all"
        self.client.exec_command(cmd=cmd, sudo=True)

    def run_deep_scrub(self, **kwargs):
        """
        Run scrub on the given OSD or on all OSD's
            Args:
                kwargs:
                1. osd : if an OSD id is passed , deep-scrub to be triggered on that osd
                        eg- obj.run_deep_scrub(osd=3)
                2. pgid: if a PGID is passed, deep-scrubs are run on that PG
                        eg- obj.run_deep_scrub(pgid=1.0)
                3. pool: if pool name is passed, deep-scrubs are run on that pool
                        eg- obj.run_deep_scrub(pool="test-pool")
            Returns: None
        """
        if kwargs.get("osd"):
            cmd = f"ceph osd deep-scrub {kwargs.get('osd')}"
        elif kwargs.get("pgid"):
            cmd = f"ceph pg deep-scrub {kwargs.get('pgid')}"
        elif kwargs.get("pool"):
            cmd = f"ceph osd pool deep-scrub {kwargs.get('pool')}"
        else:
            # scrubbing all the OSD's
            cmd = "ceph osd deep-scrub all"
        self.client.exec_command(cmd=cmd, sudo=True)

    def collect_osd_daemon_ids(self, osd_node) -> dict:
        """
        The method is used to collect the various OSD daemons present on a particular node
        :param osd_node: name of the OSD node on which osd daemon details are collected (ceph.ceph.CephNode): ceph node
        :return: list of OSD ID's
        """
        cmd = f"sudo ceph osd ls-tree {osd_node.hostname}"
        return self.run_ceph_command(cmd=cmd)

    def enable_balancer(self, **kwargs) -> bool:
        """
        Enables the balancer module with the given mode
        Args:
            kwargs: Any other args that need to be passed
            Supported kw args :
                1. balancer_mode: There are currently two supported balancer modes (str)
                   -> crush-compat
                   -> upmap (default )
                2. target_max_misplaced_ratio : the percentage of PGs that are allowed to misplaced by balancer (float)
                    target_max_misplaced_ratio = .07
                3. sleep_interval : number of seconds to sleep in between runs (int)
                    sleep_interval = 60
        Returns: True -> pass, False -> fail
        """
        # balancer is always enabled module, There is no need to enable the module via mgr.
        # To verify the same run ` ceph mgr module ls `, which would list all modules.
        # if found to be disabled, can be enabled by ` ceph mgr module enable balancer `
        mgr_modules = self.run_ceph_command(cmd="ceph mgr module ls")
        if not (
            "balancer" in mgr_modules["always_on_modules"]
            or "balancer" in mgr_modules["enabled_modules"]
        ):
            log.error(
                f"Balancer is not enabled. Enabled modules on cluster are:"
                f"{mgr_modules['always_on_modules']} & "
                f"{mgr_modules['enabled_modules']}"
            )

        # Setting the mode for the balancer. Available modes: none|crush-compat|upmap
        balancer_mode = kwargs.get("balancer_mode", "upmap")
        cmd = f"ceph balancer mode {balancer_mode}"
        self.node.shell([cmd])
        # Turning on the balancer on the system
        cmd = "ceph balancer on"
        self.node.shell([cmd])

        if kwargs.get("target_max_misplaced_ratio"):
            cmd = f"ceph config set mgr target_max_misplaced_ratio {kwargs.get('target_max_misplaced_ratio')}"
            self.node.shell([cmd])

        if kwargs.get("sleep_interval"):
            cmd = f"ceph config set mgr mgr/balancer/sleep_interval {kwargs.get('sleep_interval')}"
            self.node.shell([cmd])

        # Sleeping for 10 seconds after enabling balancer and then collecting the evaluation status
        time.sleep(10)
        cmd = "ceph balancer status"
        out = self.run_ceph_command(cmd)
        if not out["active"]:
            log.error("Exception balancer is not active")
            return False
        log.info(f"the balancer status is \n {out}")
        return True

    def check_file_exists_on_client(self, loc) -> bool:
        """Method to check if a particular file/ directory exists on the ceph client node

         Args::
            loc: Location from where the file needs to be checked
        Examples::
            status = obj.check_file_exists_on_client(loc="/tmp/crush.map.bin")
        Returns::
            True -> File exists
            False -> FIle does not exist
        """
        try:
            out, err = self.client.exec_command(cmd=f"ls {loc}", sudo=True)
            if not out:
                log.error(f"file : {loc} not present on the Client")
                return False
            log.debug(f"file : {loc} present on the Client")
            return True
        except Exception:
            log.error(f"Unable to fetch details for {log}")
            return False

    def configure_pg_autoscaler(self, **kwargs) -> bool:
        """
        Configures pg_Autoscaler as a global parameter and on pools
        Args:
            **kwargs: Any other param that needs to be set
                1. mon_target_pg_per_osd -> Sets the target number of PG's per OSD
                2. pool_config -> Config to be changed on the given pool (dict)
                    for supported args, look autoscaler_pool_settings() doc
                3. pg_autoscale_value -> Mode of pg auto-scaling to be set, if pool name is provided (str)
                    the allowed values are :
                    1. off -> turns off PG autoscaler on the given pool
                    2. warn -> displays warnings in ceph status, but does not trigger autoscale
                    3. on -> automatically autoscale based on PG count in pool
                4. default_mode -> Default mode to be set for all the newly created pools on the cluster (str)
                    the allowed values are :
                    1. off -> turns off PG autoscaler globally for subsequent pools
                    2. warn -> displays warnings in ceph status, but does not trigger autoscale
                    3. on -> automatically autoscale based on PG count in pool
        Returns: True -> pass, False -> fail
        """

        mgr_modules = self.run_ceph_command(cmd="ceph mgr module ls")
        if "pg_autoscaler" not in mgr_modules["enabled_modules"]:
            cmd = "ceph mgr module enable pg_autoscaler"
            self.node.shell([cmd])

        if kwargs.get("pool_config"):
            pool_conf = kwargs.get("pool_config")
            if not self.autoscaler_pool_settings(**pool_conf):
                return False

        if kwargs.get("default_mode"):
            cmd = f"ceph config set global osd_pool_default_pg_autoscale_mode {kwargs.get('default_mode')}"
            self.node.shell([cmd])

        if kwargs.get("mon_target_pg_per_osd"):
            cmd = f"ceph config set global mon_target_pg_per_osd {kwargs['mon_target_pg_per_osd']}"
            self.node.shell([cmd])

        cmd = "ceph osd pool autoscale-status"
        log.info(self.run_ceph_command(cmd))
        return True

    def autoscaler_pool_settings(self, **kwargs):
        """
        Sets various options on pools wrt PG Autoscaler
        Args:
            **kwargs: various kwargs to be sent
                Supported kw args:
                1. pg_autoscale_mode: PG saler mode for the indivudial pool. Values-> on, warn, off. (str)
                2. target_size_ratio: ratio of cluster pool will utilize. Values -> 0 - 1. (float)
                3. target_size_bytes: size the pool is assumed to utilize. eg: 10T (str)
                4. pg_num_min: minimum pg's for a pool. (int)
        Returns:
        """
        pool_name = kwargs["pool_name"]
        value_map = {
            "pg_autoscale_mode": kwargs.get("pg_autoscale_mode"),
            "target_size_ratio": kwargs.get("target_size_ratio"),
            "target_size_bytes": kwargs.get("target_size_bytes"),
            "pg_num_min": kwargs.get("pg_num_min"),
        }
        for val in value_map.keys():
            if val in kwargs.keys():
                if not self.set_pool_property(
                    pool=pool_name, props=val, value=value_map[val]
                ):
                    log.error(f"failed to set property {val} on pool {pool_name}")
                    return False
        return True

    def set_cluster_configuration_checks(self, **kwargs) -> bool:
        """
        Sets up Cephadm to periodically scan each of the hosts in the cluster, and to understand the state of the OS,
         disks, NICs etc
         ref doc : https://docs.ceph.com/en/latest/cephadm/operations/#cluster-configuration-checks
        Args:
            kwargs: Any other param that needs to passed
            The various args that can be sent are :
            1. disable_check_list : list of config checks that need to be disabled. (list)
            2. enable_check_list : list of config checks that need to be Enabled. (list)
            The allowed list of configuration values that can be sent are :
            1. kernel_security : checks SELINUX/Apparmor profiles are consistent across cluster hosts
            2. os_subscription : checks subscription states are consistent for all cluster hosts
            3. public_network : check that all hosts have a NIC on the Ceph public_netork
            4. osd_mtu_size : check that OSD hosts share a common MTU setting
            5. osd_linkspeed : check that OSD hosts share a common linkspeed
            6. network_missing : checks that the cluster/public networks defined exist on the Ceph hosts
            7. ceph_release : check for Ceph version consistency - ceph daemons should be on the same release
            8. kernel_version :  checks that the MAJ.MIN of the kernel on Ceph hosts is consistent
        Returns: True -> pass, False -> fail
        """

        # Checking if the checks are enabled on cluster
        cmd = "ceph cephadm config-check status"
        out, err = self.node.shell([cmd])
        if not re.search("Enabled", out):
            log.info("Cluster config checks not enabled, Proceeding to enable them")
            cmd = "ceph config set mgr mgr/cephadm/config_checks_enabled true"
            self.node.shell([cmd])

        if kwargs.get("disable_check_list"):
            if not self.disable_configuration_checks(kwargs.get("disable_check_list")):
                log.error("failed to disable the given checks")
                return False

        if kwargs.get("enable_check_list"):
            if not self.enable_configuration_checks(kwargs.get("enable_check_list")):
                log.error("failed to enable the given checks")
                return False
        log.info("Completed setting the config checks ")
        return True

    def enable_configuration_checks(self, configs: list) -> bool:
        """
        Enables checks for the configs provided
        Note: Once enabled the module, all the config checks are enabled by default
        Args:
            configs: list of config checks that need to be Enabled. (list)
        Returns: True -> Pass, False -> fail
        """
        for check in configs:
            cmd = f"ceph cephadm config-check enable {check}"
            self.node.shell([cmd])

        cmd = "ceph cephadm config-check ls"
        all_conf_checks = self.run_ceph_command(cmd)

        changed = [entry for entry in all_conf_checks if entry["name"] in configs]
        for check in changed:
            if check["status"] != "enabled":
                return False
        return True

    def disable_configuration_checks(self, configs: list) -> bool:
        """
        disables checks for the configs provided
        Note: Once enabled the module, all the config checks are enabled by default
        Args:
            configs: list of config checks that need to be disabled. (list)
        Returns: True -> Pass, False -> fail
        """
        for check in configs:
            cmd = f"ceph cephadm config-check disable {check}"
            self.node.shell([cmd])

        cmd = "ceph cephadm config-check ls"
        all_conf_checks = self.run_ceph_command(cmd)

        changed = [entry for entry in all_conf_checks if entry["name"] in configs]
        for check in changed:
            if check["status"] == "enabled":
                return False
        return True

    def reweight_crush_items(self, **kwargs) -> bool:
        """
        Performs Re-weight of various CRUSH items, based on key-value pairs sent
        Args:
            **kwargs: Arguments for the commands
        Returns: True -> pass, False -> fail
        """
        # Collecting OSD utilization before re-weights
        cmd = "ceph osd df tree"
        out = self.run_ceph_command(cmd=cmd)
        osd_info_init = [entry for entry in out["nodes"] if entry["type"] == "osd"]
        affected_osds = []
        if kwargs.get("name"):
            name = kwargs["name"]
            weight = kwargs["weight"]
            cmd = f"ceph osd crush reweight {name} {weight}"
            out = self.run_ceph_command(cmd=cmd)
            affected_osds.append(name)

        else:
            # if no params are provided, Doing the re-balance by utilization.
            cmd = r"ceph osd reweight-by-utilization"
            out = self.run_ceph_command(cmd=cmd)
            if int(out["max_change_osds"]) >= 1:
                affected_osds = [entry["osd"] for entry in out["reweights"]]
                log.info(
                    f"re-weights have been triggered on these OSD's, Deatils\n"
                    f"PG's affected : {out['utilization']['moved_pgs']}\n"
                    f"OSd's affected: {[entry for entry in out['reweights']]}"
                )
                # Sleeping for 5 seconds after command execution for process to start
                time.sleep(5)
            else:
                log.info(
                    "No re-weights based on utilization were triggered. PG distribution is optimal"
                )
                return True

        if kwargs.get("verify_reweight"):
            if not self.verify_reweight(
                affected_osds=affected_osds, osd_info=osd_info_init
            ):
                log.error("OSD utilization was not reduced upon re-weight")
                return False
        log.info("Completed the re-weight of OSD's")
        return True

    def verify_reweight(self, affected_osds: list, osd_info: list) -> bool:
        """
        Verifies if Re-weight of various CRUSH items reduced the data on the re-weighted OSD's
        Args:
            affected_osds: osd's whose weights were changed
            osd_info: OSD details before the re-weight was performed
        Returns: Pass -> True, Fail -> False
        """
        # Increasing backfill & recovery rate
        self.change_recovery_threads(config={}, action="set")
        end_time = datetime.datetime.now() + datetime.timedelta(seconds=1200)
        while end_time > datetime.datetime.now():
            status_report = self.run_ceph_command(cmd="ceph report")
            # Proceeding to check if all PG's are in active + clean
            for entry in status_report["num_pg_by_state"]:
                rec = (
                    "remapped",
                    "backfilling",
                )
                flag = (
                    False
                    if any(key in rec for key in entry["state"].split("+"))
                    else True
                )

            if flag:
                log.info("The recovery and back-filling of the OSD is completed")
                break
            log.info(
                f"Waiting for active + clean. Active aletrs: {status_report['health']['checks'].keys()},"
                f"PG States : {status_report['num_pg_by_state']}"
                f" checking status again in 1 minutes"
            )
            time.sleep(60)
        self.change_recovery_threads(config={}, action="rm")
        if not flag:
            log.error(
                "The cluster did not reach active + Clean After re-balancing by capacity"
            )
            return False

        # Checking OSD utilization after re-weight
        cmd = "ceph osd df tree"
        out = self.run_ceph_command(cmd=cmd)

        osd_info_end = [entry for entry in out["nodes"] if entry["id"] in affected_osds]
        for osd_end in osd_info_end:
            for osd_init in osd_info:
                if int(osd_init["id"]) == int(osd_end["id"]):
                    if int(osd_init["kb_used"]) > int(osd_end["kb_used"]):
                        log.error(
                            f"The utilization is higher for OSD : {osd_init['id']}"
                            f"end KB: {int(osd_end['kb_used'])}, init KB: {int(osd_init['kb_used'])}"
                        )
                        return False

        return True

    def detete_pool(self, pool: str) -> bool:
        """
        Deletes the given pool from the cluster
        Args:
            pool: name of the pool to be deleted
        Returns: True -> pass, False -> fail
        """
        # Checking if config is set to allow pool deletion
        config_dump = self.run_ceph_command(cmd="ceph config dump", client_exec=True)
        if "mon_allow_pool_delete" not in [conf["name"] for conf in config_dump]:
            cmd = "ceph config set mon mon_allow_pool_delete true"
            self.client.exec_command(cmd=cmd, sudo=True)

        existing_pools = self.run_ceph_command(cmd="ceph df", client_exec=True)
        if pool not in [ele["name"] for ele in existing_pools["pools"]]:
            log.error(f"Pool:{pool} does not exist on cluster, cannot delete")
            return True

        cmd = f"ceph osd pool delete {pool} {pool} --yes-i-really-really-mean-it"
        self.client.exec_command(cmd=cmd, sudo=True)

        existing_pools = self.run_ceph_command(cmd="ceph df", client_exec=True)
        if pool not in [ele["name"] for ele in existing_pools["pools"]]:
            log.info(f"Pool:{pool} deleted Successfully")
            return True
        log.error(f"Pool:{pool} could not be deleted on cluster")
        return False

    def enable_file_logging(self) -> bool:
        """
        Enables the cluster logging into files at var/log/ceph and checks file permissions
        Returns: True -> pass, False -> fail
        """
        try:
            cmd = "ceph config set global log_to_file true"
            self.node.shell([cmd])
            cmd = "ceph config set global mon_cluster_log_to_file true"
            self.node.shell([cmd])
        except Exception:
            log.error("Error while enabling config to log into file")
            return False
        return True

    def create_erasure_pool(self, name: str, **kwargs) -> bool:
        """
        Creates an erasure code profile and then creates a pool with the same
        References: https://docs.ceph.com/en/latest/rados/operations/erasure-code/
        Args:
            name: Name of the profile to create
            **kwargs: Any other param that needs to be set in the EC profile
                1. k -> the number of data chunks (int)
                2. m -> the number of coding chunks (int)
                3. l -> Group the coding and data chunks into sets of size locality (int)
                4. d -> Number of OSDs requested to send data during recovery of a single chunk
                        d needs to be chosen such that k+1 <= d <= k+m-1. (int)
                4. crush-failure-domain -> crush object to be us to store replica sets (str)
                5. plugin -> plugin to be set (str)
                    supported plugins:
                    1. jerasure (default)
                    2. lrc -> Upstream Only
                    3. clay -> Upstream Only
                6. pool_name -> pool name to create and associate with the EC profile being created
                7. force -> Override an existing profile by the same name.
        Returns: True -> pass, False -> fail
        """
        failure_domain = kwargs.get("crush-failure-domain", "osd")
        k = kwargs.get("k", 4)
        m = kwargs.get("m", 2)
        l = kwargs.get("l")
        d = kwargs.get("d", 5)
        plugin = kwargs.get("plugin", "jerasure")
        pool_name = kwargs.get("pool_name")
        force = kwargs.get("force")
        profile_name = f"ecprofile_{name}"

        # Creating an erasure coded profile with the options provided
        cmd = (
            f"ceph osd erasure-code-profile set {profile_name}"
            f" crush-failure-domain={failure_domain} k={k} m={m} plugin={plugin}"
        )

        if plugin == "lrc":
            cmd = cmd + f" l={l}"
        if plugin == "clay":
            cmd = cmd + f" d={d}"
        if force:
            cmd = cmd + " --force"
        try:
            self.node.shell([cmd])
        except Exception as err:
            log.error(f"Failed to create ec profile : {profile_name}")
            log.error(err)
            return False

        cmd = f"ceph osd erasure-code-profile get {profile_name}"
        log.info(self.node.shell([cmd]))
        # Creating the pool with the profile created
        if not self.create_pool(
            ec_profile_name=profile_name,
            **kwargs,
        ):
            log.error(f"Failed to create Pool {pool_name}")
            return False
        log.info(f"Created the ec profile : {profile_name} and pool : {pool_name}")
        return True

    def change_osd_state(self, action: str, target: int, timeout: int = 180) -> bool:
        """
        Changes the state of the OSD daemons wrt the action provided
        Args:
            action: operation to be performed on the service, i.e.
            start, stop, restart, disable, enable
            target: ID osd the target OSD
            timeout: timeout in seconds, (default = 60s)
        Returns: Pass -> True, Fail -> False
        """
        cluster_fsid = self.run_ceph_command(cmd="ceph fsid")["fsid"]
        host = self.fetch_host_node(daemon_type="osd", daemon_id=str(target))
        if not host:
            log.error("failed to find host for the osd")
            return False
        log.debug(f"Hostname of target host : {host.hostname}")
        init_time, _ = host.exec_command(cmd="sudo date '+%Y-%m-%d %H:%M:%S'")
        pass_status = True
        osd_status, status_desc = self.get_daemon_status(
            daemon_type="osd", daemon_id=target
        )

        if ((osd_status == 0 or status_desc == "stopped") and action == "stop") or (
            (osd_status == 1 or status_desc == "running") and action == "start"
        ):
            log.info(f"OSD {target} already in desired state: {action}")
            return True

        # If the OSD is stopped and started multiple times, the fail-count can increase
        # and the service cannot come up, without resetting the fail-count of the service.

        # Executing command to reset the fail count on the host and sleeping for 5 seconds
        cmd = "systemctl reset-failed"
        host.exec_command(sudo=True, cmd=cmd)
        time.sleep(5)

        # Executing command to perform desired action.
        cmd = f"systemctl {action} ceph-{cluster_fsid}@osd.{target}.service"
        log.info(
            f"Performing {action} on osd-{target} on host {host.hostname}. Command {cmd}"
        )
        host.exec_command(sudo=True, cmd=cmd)
        # verifying the osd state
        if action in ["start", "stop"]:
            start_time = datetime.datetime.now()
            timeout_time = start_time + datetime.timedelta(seconds=timeout)

            while datetime.datetime.now() <= timeout_time:
                osd_status, status_desc = self.get_daemon_status(
                    daemon_type="osd", daemon_id=target
                )
                log.info(f"osd_status: {osd_status}, status_desc: {status_desc}")
                if (osd_status == 0 or status_desc == "stopped") and action == "stop":
                    break
                elif (
                    osd_status == 1 or status_desc == "running"
                ) and action == "start":
                    break
                time.sleep(20)

            if action == "stop" and osd_status != 0:
                log.error(f"Failed to stop the OSD.{target} service on {host.hostname}")
                pass_status = False
            if action == "start" and osd_status != 1:
                log.error(
                    f"Failed to start the OSD.{target} service on {host.hostname}"
                )
                pass_status = False
            if not pass_status:
                log.error(
                    f"Collecting the journalctl logs for OSD.{target} service on {host.hostname} for the failure"
                )
                end_time, _ = host.exec_command(cmd="sudo date '+%Y-%m-%d %H:%M:%S'")
                osd_log_lines = self.get_journalctl_log(
                    start_time=init_time,
                    end_time=end_time,
                    daemon_type="osd",
                    daemon_id=str(target),
                )
                log.error(
                    f"\n\n ------------ Log lines from journalctl ---------------- \n"
                    f"{osd_log_lines}\n\n"
                )
                return False
        else:
            # Baremetal systems take some time for daemon restarts. changing sleep accordingly
            time.sleep(20)
        return True

    def fetch_host_node(self, daemon_type: str, daemon_id: str = None) -> object:
        """
        Provides the Ceph cluster object for the given daemon. ceph_cluster
        Args:
            daemon_type: type of daemon
                Allowed values: alertmanager, crash, mds, mgr, mon, osd, rgw, prometheus, grafana, node-exporter
            daemon_id: name of the daemon, ID in case of OSD's

        Returns: ceph object for the node

        """
        host_nodes = self.ceph_cluster.get_nodes()
        cmd = f"ceph orch ps --daemon_type {daemon_type}"
        if daemon_id is not None:
            cmd += f" --daemon_id {daemon_id}"
        daemons = self.run_ceph_command(cmd=cmd)
        try:
            o_node = [entry["hostname"] for entry in daemons][0]
            for node in host_nodes:
                if (
                    re.search(o_node, node.hostname)
                    or re.search(o_node, node.vmname)
                    or re.search(o_node, node.shortname)
                ):
                    return node
        except Exception:
            log.error(
                f"Could not find host node for daemon {daemon_type} with name {daemon_id}"
            )
            return None

    def verify_ec_overwrites(self, **kwargs) -> bool:
        """
        Creates RBD image on overwritten EC pool & replicated metadata pool
        Args:
            **kwargs: various kwargs to be sent
                Supported kw args:
                    1. image_name : name of the RBD image
                    2. image_size : size of the RBD image
                    3. metadata_pool: Name of the metadata pool to be created
        Returns: True -> pass, False -> fail

        """

        # Creating a replicated pool for metadata
        metadata_pool = kwargs.get("metadata_pool", "re_pool_overwrite")
        if metadata_pool not in self.list_pools():
            if not self.create_pool(pool_name=metadata_pool, app_name="rbd"):
                log.error("Failed to create Metadata pool for rbd images")
        pool_name = kwargs["pool_name"]
        image_name = kwargs.get("image_name", "image_ec_pool")
        image_size = kwargs.get("image_size", "40M")
        image_create = f"rbd create --size {image_size} --data-pool {pool_name} {metadata_pool}/{image_name}"
        self.node.shell([image_create])
        # tbd: create filesystem on image and mount it. Part of tire 3

        try:
            cmd = f"rbd --image {image_name} info --pool {metadata_pool}"
            out, err = self.node.shell([cmd])
            log.info(f"The image details are : {out}")
        except Exception:
            log.error("Hit error during image creation")
            return False

        # running rbd bench on the image created
        cmd = f"rbd bench-write {image_name} --pool={metadata_pool}"
        self.node.shell([cmd], check_status=False)
        return True

    def check_compression_size(self, pool_name: str, **kwargs) -> bool:
        """
        Checks the given pool size against "compression_required_ratio" and verifies that data is
        compressed in accordance to the ratio provided
        Args:
            pool_name: Name of the pool
            **kwargs: additional params needed.
                Allowed values:
                    compression_required_ratio: ratio set on the pool for compression
        Returns: True -> pass, False -> fail
        """
        log.info(f"Collecting stats about pool : {pool_name}")
        pool_stats = self.run_ceph_command(cmd="ceph df detail")["pools"]
        flag = False
        for detail in pool_stats:
            if detail["name"] == pool_name:
                pool_1_stats = detail["stats"]
                stored_data = pool_1_stats["stored_data"]
                ratio_set = kwargs["compression_required_ratio"]
                if pool_1_stats["data_bytes_used"] >= (stored_data * ratio_set):
                    log.error(
                        f"The data stored on pool is not compressed in accordance with the ratio set."
                        f"Ideal size after compression <= {stored_data * ratio_set} \n"
                        f"Stored: {pool_1_stats['data_bytes_used']}"
                    )
                    return False
                flag = True
                break
        if not flag:
            log.error(f"Pool {pool_name} not found on cluster.")
            return False
        log.info(f"data on pool is compressed in accordance of ratio : {ratio_set}")
        return True

    def do_crash_ls(self):
        """Runs clash ls on the ceph cluster. returns crash ID's if any

        Examples::
            crash_list = obj.do_crash_ls()
        """

        cmd = "ceph crash ls"
        return self.run_ceph_command(cmd=cmd)

    def get_cluster_date(self):
        """
        Used to get the osd parameter value
        Args:
            cmd: Command that needs to be run on container

        Returns : string  value
        """

        cmd = f'{"date +%Y:%m:%d:%H:%u"}'
        out, err = self.node.shell([cmd])
        return out.strip()

    def get_journalctl_log(
        self, start_time, end_time, daemon_type: str, daemon_id: str
    ) -> str:
        """
        Retrieve logs for the requested daemon using journalctl command
        Args:
            start_time: time to start reading the journalctl logs - format ('2022-07-20 09:40:10')
            end_time: time to stop reading the journalctl logs - format ('2022-07-20 10:58:49')
            daemon_type: ceph service type (mon, mgr ...)
            daemon_id: Name of the service, OSD ID in case of OSDs
        Returns:  journal_logs
        """
        fsid = self.run_ceph_command(cmd="ceph fsid")["fsid"]
        host = self.fetch_host_node(daemon_type=daemon_type, daemon_id=daemon_id)
        if daemon_type == "osd":
            systemctl_name = f"ceph-{fsid}@{daemon_type}.{daemon_id}.service"
        elif daemon_type == "mgr":
            systemctl_name = (
                f"ceph-{fsid}@{daemon_type}.{host.hostname}.{daemon_id}.service"
            )
        elif daemon_type == "mon":
            systemctl_name = f"ceph-{fsid}@{daemon_type}.{host.hostname}.service"
        else:
            systemctl_name = f"ceph-{fsid}@{daemon_type}.{host.shortname}.service"
        try:
            log_lines, err = host.exec_command(
                cmd=f"sudo journalctl -u {systemctl_name} --since '{start_time.strip()}' --until '{end_time.strip()}'"
            )
        except Exception as er:
            log.error(f"Exception hit while command execution. {er}")
        return log_lines

    def set_mclock_profile(self, profile="balanced", osd="osd", reset=False):
        """Set OSD MClock Profile.

        ceph config set osd_mclock_profile <profile_name>

        profile names:
        - balanced
        - high_recovery_ops
        - high_client_ops

        Args:
            profile: mclock profile name
            osd: "osd" service by default or "osd.<Id>"
            reset: revert mClock profile to default - balanced
        """
        if self.rhbuild and self.rhbuild.split(".")[0] < "6":
            log.info(
                f"mClock specific settings are not valid below RHCS 6"
                f", as the current RH build is {self.rhbuild}, returing TRUE"
                f" to avoid false failure"
            )
            return True
        if not self.check_osd_op_queue(qos="mclock"):
            log.error(
                "Current OSD QoS is not mclock_scheduler. \n"
                "mClock specific settings cannot be implemented"
            )
            raise Exception(
                "Failed to set mClock profile. OSD OP Queue is not mclock_scheduler"
            )
        return (
            self.node.shell([f"ceph config rm {osd} osd_mclock_profile"])
            if reset
            else self.node.shell(
                [f"ceph config set {osd} osd_mclock_profile {profile}"]
            )
        )

    def check_osd_op_queue(self, qos) -> bool:
        """Matches the input OSD op queue against the active
        Qos running on the cluster
        Args:
            qos: QoS to match [WPQ / mClock]"
        Returns:
            True if input QoS matches the active QoS, False otherwise
        """
        current_qos, _ = self.node.shell(["ceph config get osd osd_op_queue"])
        return True if qos.lower() in str(current_qos).lower() else False

    def set_mclock_parameter(
        self, param: str, value, restart_osd: bool = False
    ) -> bool:
        """Set value for any of the valid mClock config parameters
        Args:
            param (str): mClock config parameter to be modified
            value: value to be set for the input parameter
            restart_osd (boolean): flag to control restart of all OSDs;
                necessary only for few parameters, hence added as a tunable setting.
        Returns:
            boolean: True if mClock parameter was set, False otherwise
        """
        if self.rhbuild and self.rhbuild.split(".")[0] < "6":
            log.info(
                f"mClock specific settings are not valid below RHCS 6"
                f", as the current RH build is {self.rhbuild}, returing TRUE"
                f" to avoid false failure"
            )
            return True
        if not self.check_osd_op_queue(qos="mclock"):
            log.error(
                "Current OSD QoS is not mclock_scheduler. \n"
                "mClock specific settings cannot be implemented"
            )
            raise Exception(
                "Failed to set mClock profile. OSD OP Queue is not mclock_scheduler"
            )
        self.node.shell(
            ["ceph config set osd osd_mclock_override_recovery_settings true"]
        )
        self.node.shell([f"ceph config set osd {param} {value}"])
        if restart_osd:
            if not self.restart_daemon_services(daemon="osd"):
                log.error("could not restart the OSD services")
                return False
        return True

    def get_cephdf_stats(self, pool_name: str = None, detail: bool = False) -> dict:
        """
        Retrieves and returns the output ceph df command
        as a dictionary
        Args:
            pool_name: name of the pool whose stats are
            specifically required
            detail: enables ceph df detail command (default: False)
        Returns:  dictionary output of ceph df/ceph df detail
        """
        _cmd = "ceph df detail" if detail else "ceph df"
        cephdf_stats = self.run_ceph_command(cmd=_cmd, client_exec=True)

        if pool_name:
            try:
                pool_stats = cephdf_stats["pools"]
                for pool_stat in pool_stats:
                    if pool_stat.get("name") == pool_name:
                        return pool_stat
                raise KeyError
            except KeyError:
                log.error(f"{pool_name} not found in ceph df stats")
                return {}

        return cephdf_stats

    def get_pg_state(self, pg_id):
        """Function to get the current state of a PG for the specified PG ID.

        This method queries the PG to get teh current state of the PG.
        Example:
            get_pg_state(pg_id="1.f")
        Args:
            pg_id: PG id

        Returns: Pg state as a string of values
        """
        cmd = f"ceph pg {pg_id} query"
        pg_query = self.run_ceph_command(cmd=cmd)
        log.debug(f"The status of pg : {pg_id} is {pg_query['state']}")
        return pg_query["state"]

    def get_osd_map(self, pool: str, obj: str, nspace: str = None) -> dict:
        """
        Retrieve the osd map for an object in a pool
        Args:
            pool: pool name to which the object belongs
            obj: object name whose osd map is to retrieved
            nspace (optional): namespace
        Returns:  dictionary output of ceph osd map command
        """
        cmd = f"ceph osd map {pool} {obj}"
        if nspace:
            cmd += " " + nspace

        return self.run_ceph_command(cmd=cmd)

    def get_osd_df_stats(
        self, tree: bool = False, filter_by: str = None, filter: str = None
    ) -> dict:
        """
        Retrieves the output of ceph osd df command
        Args:
            tree: enables tree view
            filter_by: filter type, either class or name
            filter: a pool, crush node or device class name
        Returns: dictionary output of ceph osd df
        """
        cmd = "ceph osd df"
        if tree:
            cmd += " tree"
        if filter_by == "class" or filter_by == "name":
            cmd += " " + filter_by
        if filter:
            cmd += " " + filter

        return self.run_ceph_command(cmd=cmd)

    def get_daemon_status(self, daemon_type, daemon_id) -> tuple:
        """
        Returns the status of a specific daemon using ceph orch ps utility
        Usage: orch ps --daemon_type <> --daemon_id <>
        Args:
            daemon_type: type of daemon known to orchestrator
            daemon_id: id of daemon provided in daemon_type
        Returns: tuple containing status of the daemon (0 or 1) and
                 status description (running or stopped)
        """

        cmd_ = (
            f"ceph orch ps --daemon_type {daemon_type} "
            f"--daemon_id {daemon_id} --refresh"
        )
        orch_ps_out = self.run_ceph_command(cmd=cmd_)[0]
        log.debug(orch_ps_out)
        return orch_ps_out["status"], orch_ps_out["status_desc"]

    def get_osd_stat(self):
        """
        This Function is to get the OSD stats.
           Example:
               get_osd_stat()
           Args:
           Returns:  OSD Statistics
        """

        cmd = "ceph osd stat"
        osd_stats = self.run_ceph_command(cmd=cmd)
        log.debug(f" The OSD Statistics are : {osd_stats}")
        return osd_stats

    def get_pgid(
        self,
        pool_name: str = None,
        pool_id: int = None,
        osd: int = None,
        osd_primary: int = None,
        states: str = None,
    ) -> list:
        """
        Retrieves all the PG IDs for a pool or PG IDs where a
        certain osd is primary in the acting set or PG IDs which are
        utilizing the concerned osd
        Args:
            pool_name: name of the pool
            pool_id: pool id
            osd: osd id whose pgs are to be retrieved
            osd_primary: primary osd id whose pgs are to be retrieved
            states:
        E.g:
            cph pg ls [<pool:int>] [<states>...]
            ceph pg ls-by-osd <id|osd.id> [<pool:int>] [<states>...]
            ceph pg ls-by-pool <poolstr> [<states>...]
            ceph pg ls-by-primary <id|osd.id> [<pool:int>] [<states>...]
        Returns:
            list having pgids in string format
        """

        pgid_list = []
        cmd = "ceph pg "
        if pool_name:
            cmd += f"ls-by-pool {pool_name}"
            cmd = f"{cmd} {pool_id}" if pool_id else cmd
        elif osd:
            cmd += f"ls-by-osd {osd}"
            cmd = f"{cmd} {pool_id}" if pool_id else cmd
        elif osd_primary:
            cmd += f"ls-by-primary {osd_primary}"
            cmd = f"{cmd} {pool_id}" if pool_id else cmd
        elif pool_id:
            cmd += f"ls {pool_id}"
        else:
            log.info("No argument was provided.")
            return pgid_list

        if states:
            cmd = f"{cmd} {states}"

        pgid_dict = self.run_ceph_command(cmd=cmd)

        for pg_stats in pgid_dict["pg_stats"]:
            pgid_list.append(pg_stats["pgid"])

        return pgid_list

    def run_pool_sanity_check(self):
        """
        Runs sanity on the pools after triggering scrub and deep-scrub on pools, waiting 600 Secs

        This method is used to assess the health of Pools after any operation, where in a scrub and deep scrub is
        triggered, and the method scans the cluster for few health warnings, if generated

        Returns: True-> Pass,  false -> Fail
        """
        self.run_scrub()
        self.run_deep_scrub()
        time.sleep(10)

        end_time = datetime.datetime.now() + datetime.timedelta(seconds=600)
        flag = False
        while end_time > datetime.datetime.now():
            status_report = self.run_ceph_command(cmd="ceph report", client_exec=True)
            ceph_health_status = status_report["health"]
            health_warns = (
                "PG_AVAILABILITY",
                "PG_DEGRADED",
                "PG_RECOVERY_FULL",
                "TOO_FEW_OSDS",
                "PG_BACKFILL_FULL",
                "PG_DAMAGED",
                "OSD_SCRUB_ERRORS",
                "OSD_TOO_MANY_REPAIRS",
                "CACHE_POOL_NEAR_FULL",
                "SMALLER_PGP_NUM",
                "MANY_OBJECTS_PER_PG",
                "OBJECT_MISPLACED",
                "OBJECT_UNFOUND",
                "SLOW_OPS",
                "RECENT_CRASH",
            )

            flag = (
                False
                if any(
                    key in health_warns for key in ceph_health_status["checks"].keys()
                )
                else True
            )
            if flag:
                log.info("No warnings on the cluster")
                break

            log.info(
                f"Observing a health warning on cluster {ceph_health_status['checks'].keys()}"
            )
            time.sleep(10)

        if not flag:
            log.error(
                "Health warning generated on cluster and not cleared post waiting of 600 seconds"
            )
            return False

        log.info("Completed check on the cluster. Pass!")
        return True

    def get_osd_hosts(self):
        """
        lists the names of the OSD hosts in the cluster
        Returns: list of osd host names as used in the crush map

        """
        cmd = "ceph osd tree"
        osds = self.run_ceph_command(cmd)
        return [entry["name"] for entry in osds["nodes"] if entry["type"] == "host"]

    def change_heap_profiler_state(self, osd_list, action) -> tuple:
        """
        Start/stops the OSD heap profile
        Usage: ceph tell osd.<osd.ID> heap start_profiler
               ceph tell osd.<osd.ID> heap stop_profiler
        Args:
             osd_list: The list with the osd IDs
             action : start  or stop actions for heap profiler
        Return: tuple of exit status with the OSD list
        eg : (1, []) -> Fail
        (0, [1,2,3,4,5]) -> Pass
        """
        if not osd_list:
            log.error("OSD list is empty")
            return 1, []
        for osd_id in osd_list:
            osd_status, status_desc = self.get_daemon_status(
                daemon_type="osd", daemon_id=osd_id
            )
            if not (osd_status == 0 or status_desc == "stopped"):
                log.info(
                    f"OSD {osd_id} is in running state, enabling/Disabling Heap profiler"
                )
                cmd = f"ceph tell osd.{osd_id} heap {action}_profiler"
                self.node.shell([cmd])
            else:
                log.error(
                    f"OSD {osd_id} in stopped state. Not enabling/disabling the heap profiler on the OSD"
                )
                osd_list.remove(osd_id)
        log.info(f"The OSD {osd_list} heap profile is in {action} state")
        return 0, osd_list

    def get_heap_dump(self, osd_list):
        """
        Returns the heap dump of the all OSDs in the osd_list
        Usage: ceph tell osd.<osd.ID> heap dump
        Example:
             get_heap_dump(osd_list)
             where osd_list is the list of OSD ids like[1,2,4]
        Args:
            osd_list: The list with the osd IDs
        Return :
            A dictionary output with the key as OSD id and values are the
            heap dump of the OSD.
        """
        if not osd_list:
            log.error("OSD list is empty")
            return 1
        heap_dump = {}
        for osd_id in osd_list:
            cmd = f"ceph tell osd.{osd_id} heap dump"
            out, err = self.node.shell([cmd])
            heap_dump[osd_id] = out.strip()
        return heap_dump

    def list_orch_services(self, service_type=None) -> list:
        """
        Retrieves the list of orch services
        Args:
            service_type(optional): service name | e.g. mon, mgr, osd, etc

        Returns:
            list of service names using ceph orch ls [<service>]
        """
        service_name_ls = []
        base_cmd = "ceph orch ls"

        cmd = f"{base_cmd} {service_type}" if service_type else base_cmd
        orch_ls_op = self.run_ceph_command(cmd=cmd)

        for service in orch_ls_op:
            service_name_ls.append(service["service_name"])
        return service_name_ls

    def check_host_status(self, hostname, status: str = None) -> bool:
        """
        Checks the status of host(offline or online) using
        ceph orch host ls and return boolean
        Args:
            hostname: hostname of host to be checked
            status: custom status check for the host
        Returns:
            (bool) True -> online | False -> offline
        """
        host_cmd = f"ceph orch host ls --host_pattern {hostname} -f json"
        out, _ = self.client.exec_command(cmd=host_cmd, sudo=True)
        host_status = json.loads(out)[0]["status"].lower()
        if status and status.lower() in host_status:
            return True
        elif "offline" in host_status:
            return False
        return True

    def run_concurrent_io(self, pool_name: str, obj_name: str, obj_size: int):
        """
        Use rados put to perform concurrent IOPS on a particular object in a pool.
        Args:
            pool_name: name of the pool
            obj_name: name of the object
            obj_size: size of the object in MB
        """
        obj_name = f"{obj_name}_{obj_size}"
        installer_node = self.ceph_cluster.get_nodes(role="installer")[0]
        put_cmd = f"rados put -p {pool_name} {obj_name} /mnt/sample_1M"
        out, _ = installer_node.exec_command(
            sudo=True, cmd="truncate -s 1M ~/sample_1M"
        )
        out, _ = self.client.exec_command(
            sudo=True, cmd="truncate -s 1M /mnt/sample_1M"
        )

        def rados_put_installer(installer_offset=1048576):
            for i in range(int(obj_size / 2)):
                inst_put_cmd = f"{put_cmd} --offset {installer_offset}"
                self.node.shell(
                    args=[inst_put_cmd],
                    base_cmd_args={"mount": "~/sample_1M"},
                    check_status=False,
                )
                installer_offset = installer_offset + 2097152

        def rados_put_client(client_offset=0):
            for i in range(int(obj_size / 2)):
                client_put_cmd = f"{put_cmd} --offset {client_offset}"
                self.client.exec_command(sudo=True, cmd=client_put_cmd, check_ec=False)
                client_offset = client_offset + 2097152

        with parallel() as p:
            p.spawn(rados_put_client)
            p.spawn(rados_put_installer)

    def run_parallel_io(self, pool_name: str, obj_name: str, obj_size: int):
        """
        Use rados put to perform parallel IOPS on a particular object in a pool.
        Args:
            pool_name: name of the pool
            obj_name: name of the object
            obj_size: size of the object in MB
        """
        obj_name = f"{obj_name}_{obj_size}"
        installer_node = self.ceph_cluster.get_nodes(role="installer")[0]
        try:
            out, rc = installer_node.exec_command(
                sudo=True, cmd="rpm -qa | grep ceph-base"
            )
        except Exception:
            installer_node.exec_command(sudo=True, cmd="yum install -y ceph-base")

        put_cmd = "rados put -p $pool_name $obj_name ~/sample_1M --offset $offset"
        loop_cmd = (
            f"for ((i=1 ; i<=$END ; i++));"
            f"do {put_cmd}; export offset=$(($offset + 2097152));"
            f"done"
        )

        export_cmd = (
            f"export pool_name={pool_name} obj_name={obj_name} END={int(obj_size/2)}"
        )
        inst_run_cmd = f"{export_cmd}; export offset=1048576; {loop_cmd}"
        client_run_cmd = f"{export_cmd}; export offset=0; {loop_cmd}"

        out, _ = installer_node.exec_command(
            sudo=True, cmd="truncate -s 1M ~/sample_1M"
        )
        out, _ = self.client.exec_command(sudo=True, cmd="truncate -s 1M ~/sample_1M")

        log.info(f"Running cmd: {client_run_cmd} on {self.client.hostname}")
        self.client.exec_command(sudo=True, cmd=client_run_cmd, check_ec=False)
        log.info(f"Running cmd: {inst_run_cmd} on {installer_node.hostname}")
        installer_node.exec_command(sudo=True, cmd=inst_run_cmd)

    def get_fragmentation_score(self, osd_id) -> float:
        """
        Retrieves and returns the fragmentation score for a particular osd
        Args:
            osd_id: OSD ID
        Return:
            (float) fragmentation score for the given OSD
        """
        # fragmentation scores for OSD
        frag_cmd = f"ceph tell osd.{osd_id} bluestore allocator score block"
        return self.run_ceph_command(cmd=frag_cmd)["fragmentation_rating"]

    def check_fragmentation_score(self, osd_id) -> bool:
        """
        Checks whether fragmentation score of the given osd is within
        acceptable range (below 0.9)
        Args:
             osd_id: OSD ID
        Return:
            True -> pass, False -> Fail
        """
        log.info(f"Checking the Fragmentation score for OSD.{osd_id}")
        frag_score = self.get_fragmentation_score(osd_id=osd_id)
        log.info(f"Fragmentation score for the OSD.{osd_id} : {frag_score}")

        if 0.9 < float(frag_score) < 1.0:
            log.error(
                f"Fragmentation on osd {osd_id} is dangerously high."
                f"Ideal range 0.0 to 0.7. Actual fragmentation on OSD.{osd_id}: {frag_score}"
            )
            return False
        return True

    def get_stretch_mode_dump(self) -> dict:
        """
        retrieves the dump values for the stretch mode from the osd dump

        Return:
            Dict with the stretch mode details
            {
                'stretch_mode_enabled': False,
                'stretch_bucket_count': 0,
                'degraded_stretch_mode': 0,
                'recovering_stretch_mode': 0,
                'stretch_mode_bucket': 0
            }
        """
        cmd = "ceph osd dump"
        osd_dump = self.run_ceph_command(cmd=cmd, client_exec=True)
        stretch_details = osd_dump["stretch_mode"]
        log.debug(f"Stretch mode dump : {stretch_details}")
        return stretch_details

    def get_ceph_pg_dump(self, pg_id: str) -> dict:
        """
        Fetches ceph pg dump in json format and returns the data
        for input PG
        Args:
            pg_id: Placement Group ID for which pg dump has to be fetched

        Returns: dictionary output of ceph pg dump for input PG ID
        """
        _cmd = "ceph pg dump_json pgs"
        dump_out_str, _ = self.client.exec_command(cmd=_cmd)
        if dump_out_str.isspace():
            return {}
        dump_out = json.loads(dump_out_str)
        pg_stats = dump_out["pg_map"]["pg_stats"]
        for pg_stat in pg_stats:
            if pg_stat["pgid"] == pg_id:
                return pg_stat

        log.error(f"PG {pg_id} not found in ceph pg dump output")
        raise KeyError(f"PG {pg_id} not found in ceph pg dump output")

    def restart_daemon_services(self, daemon: str):
        """Module to restart all Orchestrator services belonging to the input
        daemon.
        Args:
            daemon (str): name of daemon whose service has to be restarted
        Returns:
            True -> Orch service restarted successfully.

            False -> One or more daemons part of the service could not restart
            within timeout
        """
        daemon_map = dict()
        success = False
        daemon_services = self.list_orch_services(service_type=daemon)
        # capture current start time for each daemon part of the services
        for service in daemon_services:
            daemon_status_ls = self.run_ceph_command(
                cmd=f"ceph orch ps --service_name {service}"
            )
            for entry in daemon_status_ls:
                start_time, _ = self.client.exec_command(
                    cmd=f"date -d {entry['started']} +'%Y%m%d%H%M%S'"
                )
                daemon_map[entry["daemon_name"]] = start_time

        # restart each service for the input daemon
        for service in daemon_services:
            self.node.shell([f"ceph orch restart {service}"])

        end_time = datetime.datetime.now() + datetime.timedelta(seconds=300)
        # wait for each daemon to restart
        for service in daemon_services:
            while datetime.datetime.now() <= end_time:
                daemon_status_ls = self.run_ceph_command(
                    cmd=f"ceph orch ps --service_name {service}"
                )
                for entry in daemon_status_ls:
                    try:
                        restart_time, _ = self.client.exec_command(
                            cmd=f"date -d {entry['started']} +'%Y%m%d%H%M%S'"
                        )
                        assert restart_time > daemon_map[entry["daemon_name"]]
                        assert entry["status_desc"] != "stopped"
                        success = True
                    except AssertionError:
                        log.info(
                            f"{daemon} daemon {entry['daemon_name']} is yet to restart. "
                            f"Sleeping for 30 secs"
                        )
                        time.sleep(30)
                        success = False
                        break
                if success:
                    break
            else:
                log.error(
                    f"All the daemons part of the service {service} did not restart within"
                    f"timeout of 5 mins"
                )
                return False

        log.info(f"Ceph Orch Service(s) {daemon_services} has been restarted")
        return True
