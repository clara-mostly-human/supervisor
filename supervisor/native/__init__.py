"""Native host support: run Supervisor directly on macOS, without containers.

On a native host the Supervisor, Home Assistant Core and apps are processes
managed by the host's service manager (launchd) instead of Docker containers.
"""

import sys

IS_MACOS = sys.platform == "darwin"

# Health checks and "unsupported system" evaluations that inspect Linux, Docker
# or systemd. They do not apply to a native macOS host, so they are skipped
# there instead of reporting every Mac as broken.
LINUX_ONLY_EVALUATIONS = frozenset(
    {
        "apparmor",
        "cgroup",
        "container",
        "dbus",
        "dns_server",
        "docker_configuration",
        "docker_version",
        "lxc",
        "network_manager",
        "operating_system",
        "os_agent",
        "os_version",
        "privileged",
        "resolved",
        "restart_policy",
        "systemd",
        "systemd_journal",
        "virtualization_image",
    }
)
LINUX_ONLY_CHECKS = frozenset(
    {
        "disabled_data_disk",
        "disk_lifetime",
        "dns_server",
        "dns_server_ipv6",
        "docker_config",
        "duplicate_os_installation",
        "multiple_data_disks",
        "network_interface_ipv4",
        "systemd_unit_failure",
    }
)
