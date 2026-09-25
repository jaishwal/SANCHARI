#!/usr/bin/env python3
"""Generate INET 4.6 NED and omnetpp.ini from SANCHARI tsn_topology.json."""
from __future__ import annotations
import argparse, json, re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

DEFAULT_NETWORK = "SANCHARI"
DEFAULT_OUTPUT = "simulations/generated"
DEFAULT_SIM_TIME = "1s"
DEFAULT_PACKAGE = "sanchari"
DEFAULT_CONFIG_NAME = "General"
DEFAULT_DESCRIPTION = "Generated TSN scenario"
DEFAULT_TIME_RESOLUTION = "ps"
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def generate_files(json_file, output_dir, network_name=DEFAULT_NETWORK, config=None):
    topology = load_topology(json_file)
    if not IDENT.fullmatch(str(network_name)):
        raise ValueError(f"Invalid network name '{network_name}'.")
    config = normalize_config(config)
    validate_config(topology, config)
    outdir = Path(output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    ned = outdir / f"{network_name}.ned"
    ini = outdir / "omnetpp.ini"
    ned.write_text(generate_ned(topology, network_name, config), encoding="utf-8")
    ini.write_text(generate_ini(topology, network_name, config), encoding="utf-8")
    return ned, ini


def load_topology(filename):
    path = Path(filename)
    if not path.exists():
        raise FileNotFoundError(f"Topology file does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in '{path}': {exc}") from exc
    validate_topology(data)
    return data


def validate_topology(data):
    if not isinstance(data, dict):
        raise ValueError("Topology JSON must contain an object.")
    devices, links, flows = data.get("devices"), data.get("links"), data.get("flows")
    if not isinstance(devices, dict):
        raise ValueError("Topology must contain a 'devices' object.")
    if not isinstance(links, list):
        raise ValueError("Topology must contain a 'links' array.")
    if not isinstance(flows, list):
        raise ValueError("Topology must contain a 'flows' array.")
    if not devices:
        raise ValueError("Topology contains no devices.")
    for name, d in devices.items():
        if not IDENT.fullmatch(str(name)):
            raise ValueError(f"Invalid device name '{name}'.")
        if d.get("type") not in {"end_system", "switch", "grandmaster"}:
            raise ValueError(f"Unsupported device type for '{name}': {d.get('type')}")
    for i, link in enumerate(links, 1):
        if link.get("source") not in devices or link.get("destination") not in devices:
            raise ValueError(f"Link {link.get('link_id', f'L{i}')} references an unknown device.")
        if link["source"] == link["destination"]:
            raise ValueError(f"Link {link.get('link_id', f'L{i}')} connects a device to itself.")
    for i, flow in enumerate(flows, 1):
        if flow.get("source") not in devices or flow.get("destination") not in devices:
            raise ValueError(f"Flow {flow.get('flow_id', i)} references an unknown device.")
        if flow["source"] == flow["destination"]:
            raise ValueError(f"Flow {flow.get('flow_id', i)} has identical source and destination.")


def normalize_config(config):
    config = config or {}
    ts = config.get("time_synchronization", {})
    if not isinstance(ts, dict):
        ts = {}
    return {
        "package": str(config.get("package", DEFAULT_PACKAGE)).strip() or DEFAULT_PACKAGE,
        "network_name": str(config.get("network_name", DEFAULT_NETWORK)).strip() or DEFAULT_NETWORK,
        "config_name": str(config.get("config_name", DEFAULT_CONFIG_NAME)).strip() or DEFAULT_CONFIG_NAME,
        "description": str(config.get("description", DEFAULT_DESCRIPTION)).strip(),
        "simulation_time": str(config.get("simulation_time", DEFAULT_SIM_TIME)).strip() or DEFAULT_SIM_TIME,
        "time_resolution": str(config.get("time_resolution", DEFAULT_TIME_RESOLUTION)).strip() or DEFAULT_TIME_RESOLUTION,
        "time_synchronization": {
            "enabled": bool(ts.get("enabled", True)),
            "method": str(ts.get("method", "gPTP / IEEE 802.1AS")).strip(),
            "grandmaster": str(ts.get("grandmaster", "")).strip(),
            "sync_interval": str(ts.get("sync_interval", "125ms")).strip(),
            "pdelay_interval": str(ts.get("pdelay_interval", "1s")).strip(),
        },
        "visualization": {
            "multi_canvas": bool(config.get("visualization", {}).get("multi_canvas", False)) if isinstance(config.get("visualization", {}), dict) else False,
        },
    }


def validate_config(topology, config):
    if not config["simulation_time"]:
        raise ValueError("Simulation time cannot be empty.")
    if not IDENT.fullmatch(config["package"]):
        raise ValueError(f"Invalid NED package name '{config['package']}'.")
    if not IDENT.fullmatch(config["network_name"]):
        raise ValueError(f"Invalid network name '{config['network_name']}'.")
    if not config["config_name"]:
        raise ValueError("INI config name cannot be empty.")
    if config["time_resolution"] not in {"ps"}:
        raise ValueError("Time resolution must be 'ps'.")
    ts = config["time_synchronization"]
    if not ts["enabled"]:
        return
    if ts["method"] != "gPTP / IEEE 802.1AS":
        raise ValueError("Only 'gPTP / IEEE 802.1AS' is supported.")
    gms = [n for n, d in topology["devices"].items() if d.get("type") == "grandmaster"]
    if not gms:
        raise ValueError("Time synchronization is enabled, but no Grandmaster exists.")
    if ts["grandmaster"] not in gms:
        raise ValueError(f"Selected Grandmaster '{ts['grandmaster']}' does not exist.")


def build_port_map(topology):
    ports = defaultdict(list)
    for i, link in enumerate(topology["links"], 1):
        lid = str(link.get("link_id", f"L{i}"))
        a, b = link["source"], link["destination"]
        pa, pb = f"eth{len(ports[a])}", f"eth{len(ports[b])}"
        ports[a].append({"link_id": lid, "peer": b, "port": pa})
        ports[b].append({"link_id": lid, "peer": a, "port": pb})
    for name, d in topology["devices"].items():
        try: declared = int(float(d.get("ports", 0)))
        except (TypeError, ValueError): declared = 0
        if declared and len(ports[name]) > declared:
            raise ValueError(f"Device '{name}' has {len(ports[name])} links but only {declared} ports.")
    return dict(ports)


def gptp_tree(topology, gm, ports):
    adj = defaultdict(list)
    for node, entries in ports.items():
        for e in entries:
            adj[node].append((e["peer"], e["port"]))

    # Store the interface on the CHILD node that points toward its parent.
    parent_port, seen = {}, {gm}
    q = deque([gm])
    while q:
        node = q.popleft()
        for peer, local_port in adj[node]:
            if peer not in seen:
                child_port = next(e["port"] for e in ports[peer] if e["peer"] == node)
                seen.add(peer)
                parent_port[peer] = child_port
                q.append(peer)

    missing = [n for n in topology["devices"] if n not in seen]
    if missing:
        raise ValueError(f"Grandmaster '{gm}' cannot reach: {', '.join(missing)}")
    result = {}
    for name, d in topology["devices"].items():
        if d["type"] == "grandmaster":
            role, slave = "MASTER_NODE", ""
            masters = [e["port"] for e in ports.get(name, [])]
        else:
            role = d.get("gptp_node_type") or ("SLAVE_NODE" if d["type"] == "end_system" else "BRIDGE_NODE")
            slave = str(d.get("gptp_slave_port", "")).strip() or parent_port.get(name, "eth0")
            masters = [] if role == "SLAVE_NODE" else [e["port"] for e in ports.get(name, []) if e["port"] != slave]
        result[name] = {"role": role, "slave_port": slave, "master_ports": masters}
    return result


def ned_type(d):
    return {"end_system": "TsnDevice", "switch": "TsnSwitch", "grandmaster": "TsnClock"}[d["type"]]


def device_features(d):
    if d["type"] == "grandmaster":
        return []
    names = [
        ("hasTimeSynchronization", "has_time_synchronization"),
        ("hasIngressTrafficFiltering", "has_ingress_traffic_filtering"),
        ("hasEgressTrafficShaping", "has_egress_traffic_shaping"),
        ("hasStreamRedundancy", "has_stream_redundancy"),
        ("hasIncomingStreams", "has_incoming_streams"),
        ("hasOutgoingStreams", "has_outgoing_streams"),
        ("hasFramePreemption", "has_frame_preemption"),
        ("hasCutthroughSwitching", "has_cutthrough_switching"),
    ]
    if d["type"] == "end_system":
        names.insert(2, ("hasEgressTrafficFiltering", "has_egress_traffic_filtering"))
    return [f"{ned} = {'true' if bool(d.get(src, False)) else 'false'}" for ned, src in names]


def generate_ned(topology, network_name, config=None):
    config = normalize_config(config)
    ports = build_port_map(topology)
    out = [
        "// Generated by SANCHARI.",
        "// Target: INET 4.6",
        "// Do not edit manually.",
        "",
        f"package {config['package']};",
        "",
        "import inet.node.tsn.TsnDevice;",
        "import inet.node.tsn.TsnSwitch;",
        "import inet.node.tsn.TsnClock;",
        "import inet.node.ethernet.EthernetLink;",
        "import inet.networklayer.configurator.ipv4.Ipv4NetworkConfigurator;",
        "import inet.visualizer.canvas.integrated.IntegratedMultiCanvasVisualizer;",
        "",
        f"network {network_name}",
        "{",
        "    parameters:",
        '        @display("bgb=1200,800");',
        "",
        "    submodules:",
        '        configurator: Ipv4NetworkConfigurator { @display("p=100,50"); }',
    ]
    if config["visualization"]["multi_canvas"]:
        out.append('        visualizer: IntegratedMultiCanvasVisualizer {')
        out.append('            @display("p=100,100");')
        out.append('        }')
    flow_counts = defaultdict(int)
    for f in topology["flows"]:
        flow_counts[f["source"]] += 1
        flow_counts[f["destination"]] += 1
    for name, d in topology["devices"].items():
        try: declared = int(float(d.get("ports", 0)))
        except (TypeError, ValueError): declared = 0
        nports = max(1, len(ports.get(name, [])), declared)
        params = [f"numEthInterfaces = {nports}"]
        if flow_counts[name]: params.append(f"numApps = {flow_counts[name]}")
        params += device_features(d)
        out.append(f"        {name}: {ned_type(d)} {{")
        for p in params: out.append(f"            {p};")
        out.append(f'            @display("p={fmt(d.get("x", 0))},{fmt(d.get("y", 0))}");')
        out.append("        }")
    out += ["", "    connections allowunconnected:"]
    for i, link in enumerate(topology["links"], 1):
        lid = str(link.get("link_id", f"L{i}"))
        a, b = link["source"], link["destination"]
        # Use INET's automatic gate-vector allocation. The actual runtime
        # interface indices are assigned by ethg++ in topology order.
        p = []
        if str(link.get("bitrate", "")).strip(): p.append(f"datarate = {link['bitrate']}")
        if str(link.get("delay", "")).strip(): p.append(f"delay = {link['delay']}s")
        if str(link.get("length", "")).strip().lower() != "auto" and str(link.get("length", "")).strip():
            p.append(f"length = {link['length']}m")
        channel = "EthernetLink" #+ (" { " + ", ".join(p) + " }" if p else "")
        out.append(f"        {a}.ethg++ <--> {channel} <--> {b}.ethg++; // {lid}")
    out += ["}", ""]
    return "\n".join(out)


def generate_ini(topology, network_name, config):
    ports = build_port_map(topology)
    ts = config["time_synchronization"]
    out = [
        "# Generated by SANCHARI.",
        "# Target: INET 4.6",
        "# Do not edit manually.",
        "",
        f"[{config['config_name']}]",
        f"network = {config['package']}.{network_name}",
        f"description = \"{config['description'].replace('\"', '\\\"')}\"" if config['description'] else None,
        f"sim-time-limit = {config['simulation_time']}",
        f"simtime-resolution = {config['time_resolution']}",
    ]
    out = [line for line in out if line is not None]
    out += ["", "# Ethernet interface bitrates"]
    link_by_id = {}
    for i, link in enumerate(topology["links"], 1): link_by_id[str(link.get("link_id", f"L{i}"))] = link
    for name, entries in ports.items():
        for e in entries:
            rate = str(link_by_id[e["link_id"]].get("bitrate", "")).strip()
            if rate: out.append(f"*.{name}.{e['port']}.bitrate = {rate}")
    if ts["enabled"]:
        tree = gptp_tree(topology, ts["grandmaster"], ports)
        out += ["", "# IEEE 802.1AS / gPTP", f"# Grandmaster: {ts['grandmaster']}"]
        for name, d in topology["devices"].items():
            if d["type"] != "grandmaster" and d.get("has_time_synchronization", False):
                out.append(f"*.{name}.hasTimeSynchronization = true")
        out.append(f"*.{ts['grandmaster']}.gptp.masterPorts = {arr(tree[ts['grandmaster']]['master_ports'])}")
        for name, d in topology["devices"].items():
            if name == ts["grandmaster"] or not d.get("has_time_synchronization", False): continue
            g = tree[name]
            out.append(f"*.{name}.gptp.gptpNodeType = \"{g['role']}\"")
            if g["slave_port"]: out.append(f"*.{name}.gptp.slavePort = \"{g['slave_port']}\"")
            if g["master_ports"]: out.append(f"*.{name}.gptp.masterPorts = {arr(g['master_ports'])}")
        for name, d in topology["devices"].items():
            if not d.get("has_time_synchronization", False): continue
            out.append(f"*.{name}.gptp.syncInterval = {ts['sync_interval']}")
            out.append(f"*.{name}.gptp.pdelayInterval = {ts['pdelay_interval']}")
        out += ["", "# SANCHARI clock / oscillator parameters"]
        for name, d in topology["devices"].items():
            if not d.get("has_time_synchronization", False): continue
            osc = str(d.get("oscillator_type", "")).strip()
            tick = str(d.get("nominal_tick_length", "")).strip()
            drift = str(d.get("drift_rate", "")).strip()
            if osc: out.append(f'*.{name}.clock.oscillator.typename = "{osc}"')
            if tick: out.append(f"*.{name}.clock.oscillator.nominalTickLength = {tick}")
            if drift and osc in {"ConstantDriftOscillator", "RandomDriftOscillator"}:
                out.append(f"*.{name}.clock.oscillator.driftRate = {drift}")
            # SANCHARI keeps these fields for future/random oscillator support.
            for key, ini_key in [("drift_change_lower", "driftRateChangeLowerLimit"), ("drift_change_upper", "driftRateChangeUpperLimit"), ("drift_change_interval", "changeInterval")]:
                value = str(d.get(key, "")).strip()
                if value and osc == "RandomDriftOscillator": out.append(f"*.{name}.clock.oscillator.{ini_key} = {value}")
    else:
        out += ["", "# Time synchronization disabled by SANCHARI."]
    if config["visualization"]["multi_canvas"]:
        out += [
            "",
            "# Visualization",
            '*.visualizer.typename = "IntegratedMultiCanvasVisualizer"',
        ]
    if topology["flows"]:
        out += ["", "# SANCHARI flows -> INET UDP applications"]
        src_index = defaultdict(int); dst_index = defaultdict(int)
        for i, f in enumerate(topology["flows"], 1):
            flow_id = f.get("flow_id", i); src, dst = f["source"], f["destination"]
            si, di = src_index[src], dst_index[dst]; src_index[src] += 1; dst_index[dst] += 1
            port = 10000 + i
            payload = max(1, int(round(float(f.get("payload_size_kb", 0)) * 1024)))
            period = f.get("stream_periodicity_microsec", 0)
            if float(period) <= 0: raise ValueError(f"Flow {flow_id} has invalid stream_periodicity_microsec.")
            out += [
                f"# Flow {flow_id}: {src} -> {dst}",
                f"# traffic_type={f.get('traffic_type', '')}, PCP/queue={f.get('pcp_queue_no', '')}, deadline={f.get('deadline_microsec', '')}us, jitter={f.get('max_jitter_microsec', '')}us, reliability={f.get('reliability', '')}, bandwidth={f.get('bandwidth_mbps', '')}Mbps",
                f'*.{src}.app[{si}].typename = "UdpBasicApp"',
                f'*.{src}.app[{si}].destAddresses = "{dst}"',
                f"*.{src}.app[{si}].destPort = {port}",
                f"*.{src}.app[{si}].messageLength = {payload}B",
                f"*.{src}.app[{si}].sendInterval = {fmt(period)}us",
                f"*.{src}.app[{si}].startTime = 0s",
                f'*.{dst}.app[{di}].typename = "UdpSink"',
                f"*.{dst}.app[{di}].localPort = {port}",
            ]
    return "\n".join(out) + "\n"


def fmt(value):
    try:
        x = float(value)
        return str(int(x)) if x.is_integer() else f"{x:.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError): return str(value)


def arr(values):
    return "[" + ", ".join('"' + str(v).replace('"', '\\"') + '"' for v in values) + "]"


def main():
    p = argparse.ArgumentParser(description="Generate INET 4.6 NED and INI from SANCHARI JSON.")
    p.add_argument("json_file", nargs="?", default="tsn_topology.json")
    p.add_argument("-o", "--output-dir", default=DEFAULT_OUTPUT)
    p.add_argument("--package", default=DEFAULT_PACKAGE)
    p.add_argument("--network-name", default=DEFAULT_NETWORK)
    p.add_argument("--config-name", default=DEFAULT_CONFIG_NAME)
    p.add_argument("--description", default=DEFAULT_DESCRIPTION)
    p.add_argument("--simulation-time", default=DEFAULT_SIM_TIME)
    p.add_argument("--time-resolution", default=DEFAULT_TIME_RESOLUTION, choices=["ps"])
    p.add_argument("--disable-time-sync", action="store_true")
    p.add_argument("--grandmaster", default="")
    p.add_argument("--sync-interval", default="125ms")
    p.add_argument("--pdelay-interval", default="1s")
    p.add_argument("--multi-canvas", action="store_true")
    a = p.parse_args()
    config = {
        "package": a.package,
        "network_name": a.network_name,
        "config_name": a.config_name,
        "description": a.description,
        "simulation_time": a.simulation_time,
        "time_resolution": a.time_resolution,
        "time_synchronization": {
            "enabled": not a.disable_time_sync,
            "method": "gPTP / IEEE 802.1AS",
            "grandmaster": a.grandmaster,
            "sync_interval": a.sync_interval,
            "pdelay_interval": a.pdelay_interval,
        },
        "visualization": {"multi_canvas": a.multi_canvas},
    }
    try:
        ned, ini = generate_files(a.json_file, a.output_dir, a.network_name, config)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}"); return 1
    print(f"NED: {ned}")
    print(f"INI: {ini}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
