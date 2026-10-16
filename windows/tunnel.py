def ssh_command(config):
    s = config.ssh
    if not s.host:
        return None
    return [str(s.executable), "-F", "NUL", "-N", "-T", "-p", str(s.port),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={s.known_hosts_file}", "-o", "GlobalKnownHostsFile=NUL",
        "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=5", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=no",
        "-i", str(s.identity_file), "-R", f"{s.remote_bind}:{s.local_target}", f"{s.user}@{s.host}"]
