import openvswitch, re, os, socket, fcntl, struct, logging, random
import ctypes, math, time
import shelve, commands
from autotest.client import utils, os_dep
from autotest.client.shared import error
import propcan, utils_misc, arch, aexpect

SYSFS_NET_PATH = "/sys/class/net"
PROCFS_NET_PATH = "/proc/net/dev"
#globals
sock = None
sockfd =None

class NetError(Exception):
    pass


class TAPModuleError(NetError):
    def __init__(self, devname, action="open", details=None):
        NetError.__init__(self, devname)
        self.devname = devname
        self.details = details

    def __str__(self):
        e_msg = "Can't %s %s" % (self.action, self.devname)
        if self.details is not None:
            e_msg += " : %s" % self.details
        return e_msg


class TAPNotExistError(NetError):
    def __init__(self, ifname):
        NetError.__init__(self, ifname)
        self.ifname = ifname

    def __str__(self):
        return "Interface %s does not exist" % self.ifname


class TAPCreationError(NetError):
    def __init__(self, ifname, details=None):
        NetError.__init__(self, ifname, details)
        self.ifname = ifname
        self.details = details

    def __str__(self):
        e_msg = "Cannot create TAP device %s" % self.ifname
        if self.details is not None:
            e_msg += ": %s" % self.details
        return e_msg


class TAPBringUpError(NetError):
    def __init__(self, ifname):
        NetError.__init__(self, ifname)
        self.ifname = ifname

    def __str__(self):
        return "Cannot bring up TAP %s" % self.ifname


class BRAddIfError(NetError):
    def __init__(self, ifname, brname, details):
        NetError.__init__(self, ifname, brname, details)
        self.ifname = ifname
        self.brname = brname
        self.details = details

    def __str__(self):
        return ("Can't add interface %s to bridge %s: %s" %
                (self.ifname, self.brname, self.details))


class BRDelIfError(NetError):
    def __init__(self, ifname, brname, details):
        NetError.__init__(self, ifname, brname, details)
        self.ifname = ifname
        self.brname = brname
        self.details = details

    def __str__(self):
        return ("Can't remove interface %s from bridge %s: %s" %
                (self.ifname, self.brname, self.details))


class IfNotInBridgeError(NetError):
    def __init__(self, ifname, details):
        NetError.__init__(self, ifname, details)
        self.ifname = ifname
        self.details = details

    def __str__(self):
        return ("Interface %s is not present on any bridge: %s" %
                (self.ifname, self.details))


class BRNotExistError(NetError):
    def __init__(self, brname, details):
        NetError.__init__(self, brname, details)
        self.brname = brname
        self.details = details

    def __str__(self):
        return ("Bridge %s does not exist: %s" % (self.brname, self.details))


class IfChangeBrError(NetError):
    def __init__(self, ifname, old_brname, new_brname, details):
        NetError.__init__(self, ifname, old_brname, new_brname, details)
        self.ifname = ifname
        self.new_brname = new_brname
        self.old_brname = old_brname
        self.details = details

    def __str__(self):
        return ("Can't move interface %s from bridge %s to bridge %s: %s" %
                (self.ifname, self.new_brname, self.oldbrname, self.details))


class IfChangeAddrError(NetError):
    def __init__(self, ifname, ipaddr, details):
        NetError.__init__(self, ifname, ipaddr, details)
        self.ifname = ifname
        self.ipaddr = ipaddr
        self.details = details

    def __str__(self):
        return ("Can't change interface IP address %s from interface %s: %s" %
                (self.ifname, self.ipaddr, self.details))


class BRIpError(NetError):
    def __init__(self, brname):
        NetError.__init__(self, brname)
        self.brname = brname

    def __str__(self):
        return ("Bridge %s doesn't have an IP address assigned. It's"
                " impossible to start dnsmasq for this bridge." %
                   (self.brname))


class HwAddrSetError(NetError):
    def __init__(self, ifname, mac):
        NetError.__init__(self, ifname, mac)
        self.ifname = ifname
        self.mac = mac

    def __str__(self):
        return "Can not set mac %s to interface %s" % (self.mac, self.ifname)


class HwAddrGetError(NetError):
    def __init__(self, ifname):
        NetError.__init__(self, ifname)
        self.ifname = ifname

    def __str__(self):
        return "Can not get mac of interface %s" % self.ifname


class VlanError(NetError):
    def __init__(self, ifname, details):
        NetError.__init__(self, ifname, details)
        self.ifname = ifname
        self.details = details

    def __str__(self):
        return ("Vlan error on interface %s: %s" %
                (self.ifname, self.details))


class VMNetError(NetError):
    def __str__(self):
        return ("VMNet instance items must be dict-like and contain "
                "a 'nic_name' mapping")


class DbNoLockError(NetError):
    def __str__(self):
        return "Attempt made to access database with improper locking"


def warp_init_del(func):
    def new_func(*args, **argkw):
        globals()["sock"] = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        globals()["sockfd"] = globals()["sock"].fileno()
        try:
            return func(*args, **argkw)
        finally:
            globals()["sock"].close()
            globals()["sock"] = None
            globals()["sockfd"] = None
    return  new_func


class Interface(object):
    ''' Class representing a Linux network device. '''

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "<%s %s at 0x%x>" % (self.__class__.__name__,
                                    self.name, id(self))

    @warp_init_del
    def up(self):
        '''
        Bring up the bridge interface. Equivalent to ifconfig [iface] up.
        '''

        # Get existing device flags
        ifreq = struct.pack('16sh', self.name, 0)
        flags = struct.unpack('16sh',
                              fcntl.ioctl(sockfd, arch.SIOCGIFFLAGS, ifreq))[1]

        # Set new flags
        flags = flags | arch.IFF_UP
        ifreq = struct.pack('16sh', self.name, flags)
        fcntl.ioctl(sockfd, arch.SIOCSIFFLAGS, ifreq)

    @warp_init_del
    def down(self):
        '''
        Bring up the bridge interface. Equivalent to ifconfig [iface] down.
        '''

        # Get existing device flags
        ifreq = struct.pack('16sh', self.name, 0)
        flags = struct.unpack('16sh',
                              fcntl.ioctl(sockfd, arch.SIOCGIFFLAGS, ifreq))[1]

        # Set new flags
        flags = flags & ~arch.IFF_UP
        ifreq = struct.pack('16sh', self.name, flags)
        fcntl.ioctl(sockfd, arch.SIOCSIFFLAGS, ifreq)

    @warp_init_del
    def is_up(self):
        '''
        Return True if the interface is up, False otherwise.
        '''
        # Get existing device flags
        ifreq = struct.pack('16sh', self.name, 0)
        flags = struct.unpack('16sh',
                              fcntl.ioctl(sockfd, arch.SIOCGIFFLAGS, ifreq))[1]

        # Set new flags
        if flags & arch.IFF_UP:
            return True
        else:
            return False

    @warp_init_del
    def get_mac(self):
        '''
        Obtain the device's mac address.
        '''
        ifreq = struct.pack('16sH14s', self.name, socket.AF_UNIX, '\x00'*14)
        res = fcntl.ioctl(sockfd, arch.SIOCGIFHWADDR, ifreq)
        address = struct.unpack('16sH14s', res)[2]
        mac = struct.unpack('6B8x', address)

        return ":".join(['%02X' % i for i in mac])

    @warp_init_del
    def set_mac(self, newmac):
        '''
        Set the device's mac address. Device must be down for this to
        succeed.
        '''
        macbytes = [int(i, 16) for i in newmac.split(':')]
        ifreq = struct.pack('16sH6B8x', self.name, socket.AF_UNIX, *macbytes)
        fcntl.ioctl(sockfd, arch.SIOCSIFHWADDR, ifreq)

    @warp_init_del
    def get_ip(self):
        """
        Get ip address of this interface
        """
        ifreq = struct.pack('16sH14s', self.name, socket.AF_INET, '\x00'*14)
        try:
            res = fcntl.ioctl(sockfd, arch.SIOCGIFADDR, ifreq)
        except IOError:
            return None
        ip = struct.unpack('16sH2x4s8x', res)[2]

        return socket.inet_ntoa(ip)

    @warp_init_del
    def set_ip(self, newip):
        """
        Set the ip address of the interface
        """
        ipbytes = socket.inet_aton(newip)
        ifreq = struct.pack('16sH2s4s8s', self.name,
                            socket.AF_INET, '\x00'*2, ipbytes, '\x00'*8)
        fcntl.ioctl(sockfd, arch.SIOCSIFADDR, ifreq)

    @warp_init_del
    def get_netmask(self):
        """
        Get ip network netmask
        """
        ifreq = struct.pack('16sH14s', self.name, socket.AF_INET, '\x00'*14)
        try:
            res = fcntl.ioctl(sockfd, arch.SIOCGIFNETMASK, ifreq)
        except IOError:
            return 0
        netmask = socket.ntohl(struct.unpack('16sH2xI8x', res)[2])

        return 32 - int(math.log(ctypes.c_uint32(~netmask).value + 1, 2))

    @warp_init_del
    def set_netmask(self, netmask):
        """
        Set netmask
        """
        netmask = ctypes.c_uint32(~((2 ** (32 - netmask)) - 1)).value
        nmbytes = socket.htonl(netmask)
        ifreq = struct.pack('16sH2si8s', self.name,
                            socket.AF_INET, '\x00'*2, nmbytes, '\x00'*8)
        fcntl.ioctl(sockfd, arch.SIOCSIFNETMASK, ifreq)

    @warp_init_del
    def get_index(self):
        '''
        Convert an interface name to an index value.
        '''
        ifreq = struct.pack('16si', self.name, 0)
        res = fcntl.ioctl(sockfd, arch.SIOCGIFINDEX, ifreq)
        return struct.unpack("16si", res)[1]

    @warp_init_del
    def get_stats(self):
        """
        Get the status information of the Interface
        """
        spl_re = re.compile("\s+")

        fp = open(PROCFS_NET_PATH)
        # Skip headers
        fp.readline()
        fp.readline()
        while True:
            data = fp.readline()
            if not data:
                return None

            name, stats_str = data.split(":")
            if name.strip() != self.name:
                continue

            stats = [int(a) for a in spl_re.split(stats_str.strip())]
            break

        titles = ["rx_bytes", "rx_packets", "rx_errs", "rx_drop", "rx_fifo",
                  "rx_frame", "rx_compressed", "rx_multicast", "tx_bytes",
                  "tx_packets", "tx_errs", "tx_drop", "tx_fifo", "tx_colls",
                  "tx_carrier", "tx_compressed"]
        return dict(zip(titles, stats))

    def is_brport(self):
        """
        Check Whether this Interface is a bridge port_to_br
        """
        path = os.path.join(SYSFS_NET_PATH, self.name)
        if os.path.exists(os.path.join(path, "brport")):
            return True
        else:
            return False


class Macvtap(Interface):
    """
    class of macvtap, base Interface
    """
    def __init__(self, tapname=None):
        if tapname == None:
            self.tapname = "macvtap" + utils_misc.generate_random_id()
        else:
            self.tapname = tapname
        Interface.__init__(self, self.tapname)


    def get_tapname(self):
        return self.tapname


    def get_device(self):
        return "/dev/tap%s" %  self.get_index()


    def ip_link_ctl(self, params, ignore_status=False):
        return utils.run(os_dep.command("ip"), timeout=10,
                         ignore_status=ignore_status, verbose=False,
                         args=params)


    def create(self, device, mode="vepa"):
        """
        Create a macvtap device, only when the device does not exist.

        @param device: Macvtap device to be created.
        @param mode: Creation mode.
        """
        path = os.path.join(SYSFS_NET_PATH, self.tapname)
        if os.path.exists(path):
            return

        self.ip_link_ctl(["link", "add", "link", device, "name",
                          self.tapname,"type", "macvtap", "mode", mode])


    def delete(self):
        path = os.path.join(SYSFS_NET_PATH, self.tapname)
        if os.path.exists(path):
            self.ip_link_ctl(["link", "delete", self.tapname])


    def open(self):
        device = self.get_device()
        try:
            return os.open(device, os.O_RDWR)
        except OSError, e:
            raise TAPModuleError(device, "open", e)


def add_nic_macvtap(nic, base_interface=None):
    """
    Add a macvtap nic, if you not have a macvtap switch in you env, run
    this type case you need at least two nic on you host
    If you not assign the base_interface will use the first physical interface
    which is not a brport and up to create macvtap
    """
    tap_base_device = None

    (dev_int, _) = get_sorted_net_if()
    if not dev_int:
        raise TAPCreationError("Cannot get a physical interface on the host")

    if base_interface:
        if not base_interface in dev_int:
            err_msg = "Macvtap must created base on a physical interface"
            raise TAPCreationError(err_msg)
        base_inter = Interface(base_interface)
        if (not base_inter.is_brport()) and base_inter.is_up():
            tap_base_device = base_interface
    else:
        for interface in dev_int:
            base_inter = Interface(interface)
            if base_inter.is_brport():
                continue
            if base_inter.is_up():
                tap_base_device = interface
                break

    if not tap_base_device:
        err_msg = ("Could not find a valid physical interface to create "
                   "macvtap, make sure the interface is up and it does not "
                   "belong to any bridge.")
        raise TAPCreationError(nic.ifname, err_msg)

    tap = Macvtap(nic.ifname)
    tap.create(tap_base_device)
    tap.set_mac(nic.mac)
    nic.tapfd = tap.open()
    time.sleep(3)
    tap.up()



class Bridge(object):
    def get_structure(self):
        """
        Get bridge list.
        """
        ebr_i = re.compile("^(\S+).*?\s+$", re.MULTILINE)
        br_i = re.compile("^(\S+).*?(\S+)$", re.MULTILINE)
        nbr_i = re.compile("^\s+(\S+)$", re.MULTILINE)
        out_line = (utils.run("brctl show", verbose=False).stdout.splitlines())
        result = dict()
        bridge = None
        iface = None

        for line in out_line[1:]:
            br_line = ebr_i.findall(line)
            if br_line:
                (tmpbr) = br_line[0]
                bridge = tmpbr
                result[bridge] = []
            else:
                br_line = br_i.findall(line)
                if br_line:
                    (tmpbr, iface) = br_i.findall(line)[0]
                    bridge = tmpbr
                    result[bridge] = []
                else:
                    if_line = nbr_i.findall(line)
                    if if_line:
                        iface = if_line[0]

            if iface and iface not in ['yes', 'no']:  # add interface to bridge
                result[bridge].append(iface)

        return result


    def list_br(self):
        return self.get_structure().keys()


    def port_to_br(self, port_name):
        """
        Return bridge which contain port.

        @param port_name: Name of port.
        @return: Bridge name or None if there is no bridge which contain port.
        """
        bridge = None
        for (br, ifaces) in self.get_structure().iteritems():
            if port_name in ifaces:
                bridge = br
        return bridge


    def _br_ioctl(self, io_cmd, brname, ifname):
        ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
        index = if_nametoindex(ifname)
        if index == 0:
            raise TAPNotExistError(ifname)
        ifr = struct.pack("16si", brname, index)
        _ = fcntl.ioctl(ctrl_sock, io_cmd, ifr)
        ctrl_sock.close()


    def add_port(self, brname, ifname):
        """
        Add a device to bridge

        @param ifname: Name of TAP device
        @param brname: Name of the bridge
        """
        try:
            self._br_ioctl(arch.SIOCBRADDIF, brname, ifname)
        except IOError, details:
            raise BRAddIfError(ifname, brname, details)


    def del_port(self, brname, ifname):
        """
        Remove a TAP device from bridge

        @param ifname: Name of TAP device
        @param brname: Name of the bridge
        """
        try:
            self._br_ioctl(arch.SIOCBRDELIF, brname, ifname)
        except IOError, details:
            raise BRDelIfError(ifname, brname, details)


def __init_openvswitch(func):
    """
    Decorator used for late init of __ovs variable.
    """
    def wrap_init(*args, **kargs):
        global __ovs
        if __ovs is None:
            try:
                __ovs = openvswitch.OpenVSwitchSystem()
                __ovs.init_system()
                if (not __ovs.check()):
                    raise Exception("Check of OpenVSwitch failed.")
            except Exception, e:
                logging.debug("System not support OpenVSwitch:")
                logging.debug(e)

        return func(*args, **kargs)
    return wrap_init


#Global variable for OpenVSwitch
__ovs = None
__bridge = Bridge()


def if_nametoindex(ifname):
    """
    Map an interface name into its corresponding index.
    Returns 0 on error, as 0 is not a valid index

    @param ifname: interface name
    """
    ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
    ifr = struct.pack("16si", ifname, 0)
    r = fcntl.ioctl(ctrl_sock, arch.SIOCGIFINDEX, ifr)
    index = struct.unpack("16si", r)[1]
    ctrl_sock.close()
    return index


def vnet_hdr_probe(tapfd):
    """
    Check if the IFF_VNET_HDR is support by tun.

    @param tapfd: the file descriptor of /dev/net/tun
    """
    u = struct.pack("I", 0)
    try:
        r = fcntl.ioctl(tapfd, arch.TUNGETFEATURES, u)
    except OverflowError:
        logging.debug("Fail to get tun features!")
        return False
    flags = struct.unpack("I", r)[0]
    if flags & arch.IFF_VNET_HDR:
        return True
    else:
        return False


def open_tap(devname, ifname, vnet_hdr=True):
    """
    Open a tap device and returns its file descriptor which is used by
    fd=<fd> parameter of qemu-kvm.

    @param ifname: TAP interface name
    @param vnet_hdr: Whether enable the vnet header
    """
    try:
        tapfd = os.open(devname, os.O_RDWR)
    except OSError, e:
        raise TAPModuleError(devname, "open", e)
    flags = arch.IFF_TAP | arch.IFF_NO_PI
    if vnet_hdr and vnet_hdr_probe(tapfd):
        flags |= arch.IFF_VNET_HDR

    ifr = struct.pack("16sh", ifname, flags)
    try:
        r = fcntl.ioctl(tapfd, arch.TUNSETIFF, ifr)
    except IOError, details:
        raise TAPCreationError(ifname, details)
    ifname = struct.unpack("16sh", r)[0].strip("\x00")
    return tapfd


def is_virtual_network_dev(dev_name):
    """
    @param dev_name: Device name.

    @return: True if dev_name is in virtual/net dir, else false.
    """
    if dev_name in os.listdir("/sys/devices/virtual/net/"):
        return True
    else:
        return False


def find_dnsmasq_listen_address():
    """
    Search all dnsmasq listen addresses.

    @param bridge_name: Name of bridge.
    @param bridge_ip: Bridge ip.
    @return: List of ip where dnsmasq is listening.
    """
    cmd = "ps -Af | grep dnsmasq"
    result = utils.run(cmd).stdout
    return re.findall("--listen-address (.+?) ", result, re.MULTILINE)


def local_runner(cmd, timeout=None):
    return utils.run(cmd, verbose=False, timeout=timeout).stdout


def local_runner_status(cmd, timeout=None):
    return utils.run(cmd, verbose=False, timeout=timeout).exit_status


def get_net_if(runner=None):
    """
    @param runner: command runner.
    @param div_phy_virt: if set true, will return a tuple division real
                         physical interface and virtual interface
    @return: List of network interfaces.
    """
    if runner is None:
        runner = local_runner
    cmd = "ip link"
    result = runner(cmd)
    return re.findall("^\d+: (\S+?)[@:].*$", result, re.MULTILINE)


def get_sorted_net_if():
    """
    Get all network interfaces, but sort them among physical and virtual if.

    @return: Tuple (physical interfaces, virtual interfaces)
    """
    all_interfaces = get_net_if()
    phy_interfaces = []
    vir_interfaces = []
    for d in all_interfaces:
        path = os.path.join(SYSFS_NET_PATH, d)
        if not os.path.isdir(path):
            continue
        if not os.path.exists(os.path.join(path, "device")):
            vir_interfaces.append(d)
        else:
            phy_interfaces.append(d)
    return (phy_interfaces, vir_interfaces)


def get_net_if_addrs(if_name, runner=None):
    """
    Get network device ip addresses. ioctl not used because it's not
    compatible with ipv6 address.

    @param if_name: Name of interface.
    @return: List ip addresses of network interface.
    """
    if runner is None:
        runner = local_runner
    cmd = "ip addr show %s" % (if_name)
    result = runner(cmd)
    return {"ipv4": re.findall("inet (.+?)/..?", result, re.MULTILINE),
            "ipv6": re.findall("inet6 (.+?)/...?", result, re.MULTILINE),
            "mac": re.findall("link/ether (.+?) ", result, re.MULTILINE)}


def get_net_if_and_addrs(runner=None):
    """
    @return: Dict of interfaces and their addresses {"ifname": addrs}.
    """
    ret = {}
    ifs = get_net_if(runner)
    for iface in ifs:
        ret[iface] = get_net_if_addrs(iface, runner)
    return ret


def set_net_if_ip(if_name, ip_addr, runner=None):
    """
    Get network device ip addresses. ioctl not used because there is
    incompatibility with ipv6.

    @param if_name: Name of interface.
    @param ip_addr: Interface ip addr in format "ip_address/mask".
    @raise: IfChangeAddrError.
    """
    if runner is None:
        runner = local_runner
    cmd = "ip addr add %s dev %s" % (ip_addr, if_name)
    try:
        runner(cmd)
    except error.CmdError, e:
        raise IfChangeAddrError(if_name, ip_addr, e)


def ipv6_from_mac_addr(mac_addr):
    """
    @return: Ipv6 address for communication in link range.
    """
    mp = mac_addr.split(":")
    mp[0] = ("%x") % (int(mp[0], 16) ^ 0x2)
    return "fe80::%s%s:%sff:fe%s:%s%s" % tuple(mp)


def check_add_dnsmasq_to_br(br_name, tmpdir):
    """
    Add dnsmasq for bridge. dnsmasq could be added only if bridge
    has assigned ip address.

    @param bridge_name: Name of bridge.
    @param bridge_ip: Bridge ip.
    @param tmpdir: Tmp dir for save pid file and ip range file.
    @return: When new dnsmasq is started name of pidfile  otherwise return
             None because system dnsmasq is already started on bridge.
    """
    br_ips = get_net_if_addrs(br_name)["ipv4"]
    if not br_ips:
        raise BRIpError(br_name)
    dnsmasq_listen = find_dnsmasq_listen_address()
    dhcp_ip_start = br_ips[0].split(".")
    dhcp_ip_start[3] = "128"
    dhcp_ip_start = ".".join(dhcp_ip_start)

    dhcp_ip_end = br_ips[0].split(".")
    dhcp_ip_end[3] = "254"
    dhcp_ip_end = ".".join(dhcp_ip_end)

    pidfile = ("%s-dnsmasq.pid") % (br_ips[0])
    leases = ("%s.leases") % (br_ips[0])

    if not (set(br_ips) & set(dnsmasq_listen)):
        logging.debug("There is no dnsmasq on br %s."
                      "Starting new one." % (br_name))
        utils.run("/usr/sbin/dnsmasq --strict-order --bind-interfaces"
                  " --pid-file=%s --conf-file= --except-interface lo"
                  " --listen-address %s --dhcp-range %s,%s --dhcp-leasefile=%s"
                  " --dhcp-lease-max=127 --dhcp-no-override" %
                  (os.path.join(tmpdir, pidfile), br_ips[0], dhcp_ip_start,
                   dhcp_ip_end, (os.path.join(tmpdir, leases))))
        return pidfile
    return None


@__init_openvswitch
def find_bridge_manager(br_name, ovs=None):
    """
    Finds bridge which contain interface iface_name.

    @param br_name: Name of interface.
    @return: (br_manager) which contain bridge or None.
    """
    if ovs is None:
        ovs = __ovs
    # find ifname in standard linux bridge.
    if br_name in __bridge.list_br():
        return __bridge
    elif not ovs is None and br_name in ovs.list_br():
        return ovs
    else:
        return None


@__init_openvswitch
def find_current_bridge(iface_name, ovs=None):
    """
    Finds bridge which contains interface iface_name.

    @param iface_name: Name of interface.
    @return: (br_manager, Bridge) which contain iface_name or None.
    """
    if ovs is None:
        ovs = __ovs
    # find ifname in standard linux bridge.
    master = __bridge
    bridge = master.port_to_br(iface_name)
    if bridge is None:
        master = ovs
        bridge = master.port_to_br(iface_name)

    if bridge is None:
        master = None

    return (master, bridge)


@__init_openvswitch
def change_iface_bridge(ifname, new_bridge, ovs=None):
    """
    Change bridge on which interface was added.

    @param ifname: Iface name or Iface struct.
    @param new_bridge: Name of new bridge.
    """
    if ovs is None:
        ovs = __ovs
    br_manager_new = find_bridge_manager(new_bridge, ovs)
    if br_manager_new is None:
        raise BRNotExistError(new_bridge, "")

    if type(ifname) is str:
        (br_manager_old, br_old) = find_current_bridge(ifname, ovs)
        if not br_manager_old is None:
            br_manager_old.del_port(br_old, ifname)
        br_manager_new.add_port(new_bridge, ifname)
    elif issubclass(type(ifname), VirtIface):
        br_manager_old = find_bridge_manager(ifname.netdst, ovs)
        if not br_manager_old is None:
            br_manager_old.del_port(ifname.netdst, ifname.ifname)
        br_manager_new.add_port(new_bridge, ifname.ifname)
        ifname.netdst = new_bridge
    else:
        raise error.AutotestError("Network interface %s is wrong type %s." %
                                  (ifname, new_bridge))


@__init_openvswitch
def add_to_bridge(ifname, brname, ovs=None):
    """
    Add a TAP device to bridge

    @param ifname: Name of TAP device
    @param brname: Name of the bridge
    @param ovs: OpenVSwitch object.
    """
    if ovs is None:
        ovs = __ovs

    _ifname = None
    if type(ifname) is str:
        _ifname = ifname
    elif issubclass(type(ifname), VirtIface):
        _ifname = ifname.ifname

    if brname in __bridge.list_br():
        #Try add port to standard bridge or openvswitch in compatible mode.
        __bridge.add_port(brname, _ifname)
        return

    if ovs is None:
        raise BRAddIfError(ifname, brname, "There is no bridge in system.")
    #Try add port to OpenVSwitch bridge.
    if brname in ovs.list_br():
        ovs.add_port(brname, ifname)


@__init_openvswitch
def del_from_bridge(ifname, brname, ovs=None):
    """
    Del a TAP device to bridge

    @param ifname: Name of TAP device
    @param brname: Name of the bridge
    @param ovs: OpenVSwitch object.
    """
    if ovs is None:
        ovs = __ovs

    _ifname = None
    if type(ifname) is str:
        _ifname = ifname
    elif issubclass(type(ifname), VirtIface):
        _ifname = ifname.ifname

    if ovs is None:
        raise BRDelIfError(ifname, brname, "There is no bridge in system.")

    if brname in __bridge.list_br():
        #Try add port to standard bridge or openvswitch in compatible mode.
        __bridge.del_port(brname, _ifname)
        return

    #Try add port to OpenVSwitch bridge.
    if brname in ovs.list_br():
        ovs.del_port(brname, _ifname)


def bring_up_ifname(ifname):
    """
    Bring up an interface

    @param ifname: Name of the interface
    """
    ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
    ifr = struct.pack("16sh", ifname, arch.IFF_UP)
    try:
        fcntl.ioctl(ctrl_sock, arch.SIOCSIFFLAGS, ifr)
    except IOError:
        raise TAPBringUpError(ifname)
    ctrl_sock.close()


def bring_down_ifname(ifname):
    """
    Bring up an interface

    @param ifname: Name of the interface
    """
    ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
    ifr = struct.pack("16sh", ifname, 0)
    try:
        fcntl.ioctl(ctrl_sock, arch.SIOCSIFFLAGS, ifr)
    except IOError:
        raise TAPBringUpError(ifname)
    ctrl_sock.close()


def if_set_macaddress(ifname, mac):
    """
    Set the mac address for an interface

    @param ifname: Name of the interface
    @mac: Mac address
    """
    ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)

    ifr = struct.pack("256s", ifname)
    try:
        mac_dev = fcntl.ioctl(ctrl_sock, arch.SIOCGIFHWADDR, ifr)[18:24]
        mac_dev = ":".join(["%02x" % ord(m) for m in mac_dev])
    except IOError, e:
        raise HwAddrGetError(ifname)

    if mac_dev.lower() == mac.lower():
        return

    ifr = struct.pack("16sH14s", ifname, 1,
                      "".join([chr(int(m, 16)) for m in mac.split(":")]))
    try:
        fcntl.ioctl(ctrl_sock, arch.SIOCSIFHWADDR, ifr)
    except IOError, e:
        logging.info(e)
        raise HwAddrSetError(ifname, mac)
    ctrl_sock.close()


class VirtIface(propcan.PropCan):
    """
    Networking information for single guest interface and host connection.
    """

    __slots__ = ['nic_name', 'g_nic_name', 'mac', 'nic_model', 'ip',
                 'nettype', 'netdst']
    # Make sure first byte generated is always zero and it follows
    # the class definition.  This helps provide more predictable
    # addressing while avoiding clashes between multiple NICs.
    LASTBYTE = random.SystemRandom().randint(0x00, 0xff)

    def __getstate__(self):
        state = {}
        for key in self.__class__.__slots__:
            if self.has_key(key):
                state[key] = self[key]
        return state


    def __setstate__(self, state):
        self.__init__(state)


    @classmethod
    def name_is_valid(cls, nic_name):
        """
        Corner-case prevention where nic_name is not a sane string value
        """
        try:
            return isinstance(nic_name, str) and len(nic_name) > 1
        except (TypeError, KeyError, AttributeError):
            return False


    @classmethod
    def mac_is_valid(cls, mac):
        try:
            mac = cls.mac_str_to_int_list(mac)
        except TypeError:
            return False
        return True # Though may be less than 6 bytes


    @classmethod
    def mac_str_to_int_list(cls, mac):
        """
        Convert list of string bytes to int list
        """
        if isinstance(mac, (str, unicode)):
            mac = mac.split(':')
        # strip off any trailing empties
        for rindex in xrange(len(mac), 0, -1):
            if not mac[rindex-1].strip():
                del mac[rindex-1]
            else:
                break
        try:
            assert len(mac) < 7
            for byte_str_index in xrange(0, len(mac)):
                byte_str = mac[byte_str_index]
                assert isinstance(byte_str, (str, unicode))
                assert len(byte_str) > 0
                try:
                    value = eval("0x%s" % byte_str, {}, {})
                except SyntaxError:
                    raise AssertionError
                assert value >= 0x00
                assert value <= 0xFF
                mac[byte_str_index] = value
        except AssertionError:
            raise TypeError("%s %s is not a valid MAC format "
                            "string or list" % (str(mac.__class__),
                             str(mac)))
        return mac


    @classmethod
    def int_list_to_mac_str(cls, mac_bytes):
        """
        Return string formatting of int mac_bytes
        """
        for byte_index in xrange(0, len(mac_bytes)):
            mac = mac_bytes[byte_index]
            # Project standardized on lower-case hex
            if mac < 16:
                mac_bytes[byte_index] = "0%x" % mac
            else:
                mac_bytes[byte_index] = "%x" % mac
        return mac_bytes


    @classmethod
    def generate_bytes(cls):
        """
        Return next byte from ring
        """
        cls.LASTBYTE += 1
        if cls.LASTBYTE > 0xff:
            cls.LASTBYTE = 0
        yield cls.LASTBYTE


    @classmethod
    def complete_mac_address(cls, mac):
        """
        Append randomly generated byte strings to make mac complete

        @param: mac: String or list of mac bytes (possibly incomplete)
        @raise: TypeError if mac is not a string or a list
        """
        mac = cls.mac_str_to_int_list(mac)
        if len(mac) == 6:
            return ":".join(cls.int_list_to_mac_str(mac))
        for rand_byte in cls.generate_bytes():
            mac.append(rand_byte)
            return cls.complete_mac_address(cls.int_list_to_mac_str(mac))


class LibvirtIface(VirtIface):
    """
    Networking information specific to libvirt
    """
    __slots__ = VirtIface.__slots__ + []


class QemuIface(VirtIface):
    """
    Networking information specific to Qemu
    """
    __slots__ = VirtIface.__slots__ + ['vlan', 'device_id', 'ifname', 'tapfd',
                                       'tapfd_id', 'netdev_id', 'tftp',
                                       'romfile', 'nic_extra_params',
                                       'netdev_extra_params']


class VMNet(list):
    """
    Collection of networking information.
    """

    # don't flood discard warnings
    DISCARD_WARNINGS = 10

    # __init__ must not presume clean state, it should behave
    # assuming there is existing properties/data on the instance
    # and take steps to preserve or update it as appropriate.
    def __init__(self, container_class=VirtIface, virtiface_list=[]):
        """
        Initialize from list-like virtiface_list using container_class
        """
        if container_class != VirtIface and (
                        not issubclass(container_class, VirtIface)):
            raise TypeError("Container class must be Base_VirtIface "
                            "or subclass not a %s" % str(container_class))
        self.container_class = container_class
        super(VMNet, self).__init__([])
        if isinstance(virtiface_list, list):
            for virtiface in virtiface_list:
                self.append(virtiface)
        else:
            raise VMNetError


    def __getstate__(self):
        return [nic for nic in self]


    def __setstate__(self, state):
        VMNet.__init__(self, self.container_class, state)


    def __getitem__(self, index_or_name):
        if isinstance(index_or_name, str):
            index_or_name = self.nic_name_index(index_or_name)
        return super(VMNet, self).__getitem__(index_or_name)


    def __setitem__(self, index_or_name, value):
        if not isinstance(value, dict):
            raise VMNetError
        if self.container_class.name_is_valid(value['nic_name']):
            if isinstance(index_or_name, str):
                index_or_name = self.nic_name_index(index_or_name)
            self.process_mac(value)
            super(VMNet, self).__setitem__(index_or_name,
                                           self.container_class(value))
        else:
            raise VMNetError


    def __delitem__(self, index_or_name):
        if isinstance(index_or_name, str):
            index_or_name = self.nic_name_index(index_or_name)
        super(VMNet, self).__delitem__(index_or_name)


    def subclass_pre_init(self, params, vm_name):
        """
        Subclasses must establish style before calling VMNet. __init__()
        """
        #TODO: Get rid of this function.  it's main purpose is to provide
        # a shared way to setup style (container_class) from params+vm_name
        # so that unittests can run independently for each subclass.
        self.vm_name = vm_name
        self.params = params.object_params(self.vm_name)
        self.vm_type = self.params.get('vm_type', 'default')
        self.driver_type = self.params.get('driver_type', 'default')
        for key, value in VMNetStyle(self.vm_type,
                                    self.driver_type).items():
            setattr(self, key, value)


    def process_mac(self, value):
        """
        Strips 'mac' key from value if it's not valid
        """
        original_mac = mac = value.get('mac')
        if mac:
            mac = value['mac'] = value['mac'].lower()
            if len(mac.split(':')
                            ) == 6 and self.container_class.mac_is_valid(mac):
                return
            else:
                del value['mac'] # don't store invalid macs
                # Notify user about these, but don't go crazy
                if self.__class__.DISCARD_WARNINGS >= 0:
                    logging.warning('Discarded invalid mac "%s" for nic "%s" '
                                    'from input, %d warnings remaining.'
                                    % (original_mac,
                                       value.get('nic_name'),
                                       self.__class__.DISCARD_WARNINGS))
                    self.__class__.DISCARD_WARNINGS -= 1


    def mac_list(self):
        """
        Return a list of all mac addresses used by defined interfaces
        """
        return [nic.mac for nic in self if hasattr(nic, 'mac')]


    def append(self, value):
        newone = self.container_class(value)
        newone_name = newone['nic_name']
        if newone.name_is_valid(newone_name) and (
                          newone_name not in self.nic_name_list()):
            self.process_mac(newone)
            super(VMNet, self).append(newone)
        else:
            raise VMNetError


    def nic_name_index(self, name):
        """
        Return the index number for name, or raise KeyError
        """
        if not isinstance(name, str):
            raise TypeError("nic_name_index()'s nic_name must be a string")
        nic_name_list = self.nic_name_list()
        try:
            return nic_name_list.index(name)
        except ValueError:
            raise IndexError("Can't find nic named '%s' among '%s'" %
                             (name, nic_name_list))


    def nic_name_list(self):
        """
        Obtain list of nic names from lookup of contents 'nic_name' key.
        """
        namelist = []
        for item in self:
            # Rely on others to throw exceptions on 'None' names
            namelist.append(item['nic_name'])
        return namelist


    def nic_lookup(self, prop_name, prop_value):
        """
        Return the first index with prop_name key matching prop_value or None
        """
        for nic_index in xrange(0, len(self)):
            if self[nic_index].has_key(prop_name):
                if self[nic_index][prop_name] == prop_value:
                    return nic_index
        return None


# TODO: Subclass VMNet into Qemu/Libvirt variants and
# pull them, along with ParmasNet and maybe DbNet based on
# Style definitions.  i.e. libvirt doesn't need DbNet at all,
# but could use some custom handling at the VMNet layer
# for xen networking.  This will also enable further extensions
# to network information handing in the future.
class VMNetStyle(dict):
    """
    Make decisions about needed info from vm_type and driver_type params.
    """

    # Keyd first by vm_type, then by driver_type.
    VMNet_Style_Map = {
        'default':{
            'default':{
                'mac_prefix':'9a',
                'container_class': QemuIface,
            }
        },
        'libvirt':{
            'default':{
                'mac_prefix':'9a',
                'container_class': LibvirtIface,
            },
            'qemu':{
                'mac_prefix':'52:54:00',
                'container_class': LibvirtIface,
            },
            'xen':{
                'mac_prefix':'00:16:3e',
                'container_class': LibvirtIface,
            }
        }
    }

    def __new__(cls, vm_type, driver_type):
        return cls.get_style(vm_type, driver_type)


    @classmethod
    def get_vm_type_map(cls, vm_type):
        return cls.VMNet_Style_Map.get(vm_type,
                                        cls.VMNet_Style_Map['default'])


    @classmethod
    def get_driver_type_map(cls, vm_type_map, driver_type):
        return vm_type_map.get(driver_type,
                               vm_type_map['default'])


    @classmethod
    def get_style(cls, vm_type, driver_type):
        style = cls.get_driver_type_map( cls.get_vm_type_map(vm_type),
                                         driver_type )
        return style


class ParamsNet(VMNet):
    """
    Networking information from Params

        Params contents specification-
            vms = <vm names...>
            nics = <nic names...>
            nics_<vm name> = <nic names...>
            # attr: mac, ip, model, nettype, netdst, etc.
            <attr> = value
            <attr>_<nic name> = value
    """

    # __init__ must not presume clean state, it should behave
    # assuming there is existing properties/data on the instance
    # and take steps to preserve or update it as appropriate.
    def __init__(self, params, vm_name):
        self.subclass_pre_init(params, vm_name)
        # use temporary list to initialize
        result_list = []
        nic_name_list = self.params.objects('nics')
        for nic_name in nic_name_list:
            # nic name is only in params scope
            nic_dict = {'nic_name':nic_name}
            nic_params = self.params.object_params(nic_name)
            # avoid processing unsupported properties
            proplist = list(self.container_class.__slots__)
            # nic_name was already set, remove from __slots__ list copy
            del proplist[proplist.index('nic_name')]
            for propertea in proplist:
                # Merge existing propertea values if they exist
                try:
                    existing_value = getattr(self[nic_name], propertea, None)
                except ValueError:
                    existing_value = None
                except IndexError:
                    existing_value = None
                nic_dict[propertea] = nic_params.get(propertea, existing_value)
            result_list.append(nic_dict)
        VMNet.__init__(self, self.container_class, result_list)


    def mac_index(self):
        """
        Generator over mac addresses found in params
        """
        for nic_name in self.params.get('nics'):
            nic_obj_params = self.params.object_params(nic_name)
            mac = nic_obj_params.get('mac')
            if mac:
                yield mac
            else:
                continue


    def reset_mac(self, index_or_name):
        """
        Reset to mac from params if defined and valid, or undefine.
        """
        nic = self[index_or_name]
        nic_name = nic.nic_name
        nic_params = self.params.object_params(nic_name)
        params_mac = nic_params.get('mac')
        if params_mac and self.container_class.mac_is_valid(params_mac):
            new_mac = params_mac.lower()
        else:
            new_mac = None
        nic.mac = new_mac


    def reset_ip(self, index_or_name):
        """
        Reset to ip from params if defined and valid, or undefine.
        """
        nic = self[index_or_name]
        nic_name = nic.nic_name
        nic_params = self.params.object_params(nic_name)
        params_ip = nic_params.get('ip')
        if params_ip:
            new_ip = params_ip
        else:
            new_ip = None
        nic.ip = new_ip


class DbNet(VMNet):
    """
    Networking information from database

        Database specification-
            database values are python string-formatted lists of dictionaries
    """

    # __init__ must not presume clean state, it should behave
    # assuming there is existing properties/data on the instance
    # and take steps to preserve or update it as appropriate.
    def __init__(self, params, vm_name, db_filename, db_key):
        self.subclass_pre_init(params, vm_name)
        self.db_key = db_key
        self.db_filename = db_filename
        self.db_lockfile = db_filename + ".lock"
        # Merge (don't overwrite) existing propertea values if they
        # exist in db
        try:
            self.lock_db()
            entry = self.db_entry()
        except KeyError:
            entry = []
        self.unlock_db()
        proplist = list(self.container_class.__slots__)
        # nic_name was already set, remove from __slots__ list copy
        del proplist[proplist.index('nic_name')]
        nic_name_list = self.nic_name_list()
        for db_nic in entry:
            nic_name = db_nic['nic_name']
            if nic_name in nic_name_list:
                for propertea in proplist:
                    # only set properties in db but not in self
                    if db_nic.has_key(propertea):
                        self[nic_name].set_if_none(propertea, db_nic[propertea])
        if entry:
            VMNet.__init__(self, self.container_class, entry)
        # Assume self.update_db() called elsewhere


    def lock_db(self):
        if not hasattr(self, 'lock'):
            self.lock = utils_misc.lock_file(self.db_lockfile)
            if not hasattr(self, 'db'):
                self.db = shelve.open(self.db_filename)
            else:
                raise DbNoLockError
        else:
            raise DbNoLockError


    def unlock_db(self):
        if hasattr(self, 'db'):
            self.db.close()
            del self.db
            if hasattr(self, 'lock'):
                utils_misc.unlock_file(self.lock)
                del self.lock
            else:
                raise DbNoLockError
        else:
            raise DbNoLockError


    def db_entry(self, db_key=None):
        """
        Returns a python list of dictionaries from locked DB string-format entry
        """
        if not db_key:
            db_key = self.db_key
        try:
            db_entry = self.db[db_key]
        except AttributeError: # self.db doesn't exist:
            raise DbNoLockError
        # Always wear protection
        try:
            eval_result = eval(db_entry, {}, {})
        except SyntaxError:
            raise ValueError("Error parsing entry for %s from "
                             "database '%s'" % (self.db_key,
                                                self.db_filename))
        if not isinstance(eval_result, list):
            raise ValueError("Unexpected database data: %s" % (
                                    str(eval_result)))
        result = []
        for result_dict in eval_result:
            if not isinstance(result_dict, dict):
                raise ValueError("Unexpected database sub-entry data %s" % (
                                    str(result_dict)))
            result.append(result_dict)
        return result


    def save_to_db(self, db_key=None):
        """
        Writes string representation out to database
        """
        if db_key == None:
            db_key = self.db_key
        data = str(self)
        # Avoid saving empty entries
        if len(data) > 3:
            try:
                self.db[self.db_key] = data
            except AttributeError:
                raise DbNoLockError
        else:
            try:
                # make sure old db entry is removed
                del self.db[db_key]
            except KeyError:
                pass


    def update_db(self):
        self.lock_db()
        self.save_to_db()
        self.unlock_db()


    def mac_index(self):
        """Generator of mac addresses found in database"""
        try:
            for db_key in self.db.keys():
                for nic in self.db_entry(db_key):
                    mac = nic.get('mac')
                    if mac:
                        yield mac
                    else:
                        continue
        except AttributeError:
            raise DbNoLockError


class VirtNet(DbNet, ParamsNet):
    """
    Persistent collection of VM's networking information.
    """
    # __init__ must not presume clean state, it should behave
    # assuming there is existing properties/data on the instance
    # and take steps to preserve or update it as appropriate.
    def __init__(self, params, vm_name, db_key,
                                        db_filename="/tmp/address_pool"):
        """
        Load networking info. from db, then from params, then update db.

        @param: params: Params instance using specification above
        @param: vm_name: Name of the VM as might appear in Params
        @param: db_key: database key uniquely identifying VM instance
        @param: db_filename: database file to cache previously parsed params
        """
        # Params always overrides database content
        DbNet.__init__(self, params, vm_name, db_filename, db_key)
        ParamsNet.__init__(self, params, vm_name)
        self.update_db()


    # Delegating get/setstate() details more to ancestor classes
    # doesn't play well with multi-inheritence.  While possibly
    # more difficult to maintain, hard-coding important property
    # names for pickling works. The possibility also remains open
    # for extensions via style-class updates.
    def __getstate__(self):
        state = {'container_items':VMNet.__getstate__(self)}
        for attrname in ['params', 'vm_name', 'db_key', 'db_filename',
                         'vm_type', 'driver_type', 'db_lockfile']:
            state[attrname] = getattr(self, attrname)
        for style_attr in VMNetStyle(self.vm_type, self.driver_type).keys():
            state[style_attr] = getattr(self, style_attr)
        return state


    def __setstate__(self, state):
        for key in state.keys():
            if key == 'container_items':
                continue # handle outside loop
            setattr(self, key, state.pop(key))
        VMNet.__setstate__(self, state.pop('container_items'))


    def __eq__(self, other):
        if len(self) != len(other):
            return False
        # Order doesn't matter for most OS's as long as MAC & netdst match
        for nic_name in self.nic_name_list():
            if self[nic_name] != other[nic_name]:
                return False
        return True


    def __ne__(self, other):
        return not self.__eq__(other)


    def mac_index(self):
        """
        Generator for all allocated mac addresses (requires db lock)
        """
        for mac in DbNet.mac_index(self):
            yield mac
        for mac in ParamsNet.mac_index(self):
            yield mac


    def generate_mac_address(self, nic_index_or_name, attempts=1024):
        """
        Set & return valid mac address for nic_index_or_name or raise NetError

        @param: nic_index_or_name: index number or name of NIC
        @return: MAC address string
        @raise: NetError if mac generation failed
        """
        nic = self[nic_index_or_name]
        if nic.has_key('mac'):
            logging.warning("Overwriting mac %s for nic %s with random"
                                % (nic.mac, str(nic_index_or_name)))
        self.free_mac_address(nic_index_or_name)
        attempts_remaining = attempts
        while attempts_remaining > 0:
            mac_attempt = nic.complete_mac_address(self.mac_prefix)
            self.lock_db()
            if mac_attempt not in self.mac_index():
                nic.mac = mac_attempt.lower()
                self.unlock_db()
                self.update_db()
                return self[nic_index_or_name].mac
            else:
                attempts_remaining -= 1
                self.unlock_db()
        raise NetError("%s/%s MAC generation failed with prefix %s after %d "
                       "attempts for NIC %s on VM %s (%s)" % (
                            self.vm_type,
                            self.driver_type,
                            self.mac_prefix,
                            attempts,
                            str(nic_index_or_name),
                            self.vm_name,
                            self.db_key))


    def free_mac_address(self, nic_index_or_name):
        """
        Remove the mac value from nic_index_or_name and cache unless static

        @param: nic_index_or_name: index number or name of NIC
        """
        nic = self[nic_index_or_name]
        if nic.has_key('mac'):
            # Reset to params definition if any, or None
            self.reset_mac(nic_index_or_name)
        self.update_db()


    def set_mac_address(self, nic_index_or_name, mac):
        """
        Set a MAC address to value specified

        @param: nic_index_or_name: index number or name of NIC
        @raise: NetError if mac already assigned
        """
        nic = self[nic_index_or_name]
        if nic.has_key('mac'):
            logging.warning("Overwriting mac %s for nic %s with %s"
                            % (nic.mac, str(nic_index_or_name), mac))
        nic.mac = mac.lower()
        self.update_db()


    def get_mac_address(self, nic_index_or_name):
        """
        Return a MAC address for nic_index_or_name

        @param: nic_index_or_name: index number or name of NIC
        @return: MAC address string.
        """
        return self[nic_index_or_name].mac.lower()


    def generate_ifname(self, nic_index_or_name):
        """
        Return and set network interface name
        """
        nic_index = self.nic_name_index(self[nic_index_or_name].nic_name)
        prefix = "t%d-" % nic_index
        postfix = utils_misc.generate_random_string(6)
        # Ensure interface name doesn't excede 11 characters
        self[nic_index_or_name].ifname = (prefix + postfix)[-11:]
        self.update_db()
        return self[nic_index_or_name].ifname


def parse_arp():
    """
    Read /proc/net/arp, return a mapping of MAC to IP

    @return: dict mapping MAC to IP
    """
    ret = {}
    arp_cache = file('/proc/net/arp').readlines()

    for line in arp_cache:
        mac = line.split()[3]
        ip = line.split()[0]

        # Skip the header
        if mac.count(":") != 5:
            continue

        ret[mac] = ip

    return ret


def verify_ip_address_ownership(ip, macs, timeout=10.0):
    """
    Use arping and the ARP cache to make sure a given IP address belongs to one
    of the given MAC addresses.

    @param ip: An IP address.
    @param macs: A list or tuple of MAC addresses.
    @return: True if ip is assigned to a MAC address in macs.
    """
    ip_map = parse_arp()
    for mac in macs:
        if ip_map.get(mac) == ip:
            return True

    # Compile a regex that matches the given IP address and any of the given
    # MAC addresses
    mac_regex = "|".join("(%s)" % mac for mac in macs)
    regex = re.compile(r"\b%s\b.*\b(%s)\b" % (ip, mac_regex), re.IGNORECASE)

    # Get the name of the bridge device for arping
    o = commands.getoutput("%s route get %s" % (utils_misc.find_command("ip"), ip))
    dev = re.findall("dev\s+\S+", o, re.IGNORECASE)
    if not dev:
        return False
    dev = dev[0].split()[-1]

    # Send an ARP request
    o = commands.getoutput("%s -f -c 3 -I %s %s" %
                           (utils_misc.find_command("arping"), dev, ip))
    return bool(regex.search(o))


def generate_mac_address_simple():
    r = random.SystemRandom()
    mac = "9a:%02x:%02x:%02x:%02x:%02x" % (r.randint(0x00, 0xff),
                                           r.randint(0x00, 0xff),
                                           r.randint(0x00, 0xff),
                                           r.randint(0x00, 0xff),
                                           r.randint(0x00, 0xff))
    return mac


def get_ip_address_by_interface(ifname):
    """
    returns ip address by interface
    @param ifname - interface name
    @raise NetError - When failed to fetch IP address (ioctl raised IOError.).

    Retrieves interface address from socket fd trough ioctl call
    and transforms it into string from 32-bit packed binary
    by using socket.inet_ntoa().

    """
    mysocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return socket.inet_ntoa(fcntl.ioctl(
                    mysocket.fileno(),
                    arch.SIOCGIFADDR,
                    struct.pack('256s', ifname[:15]) # ifname to binary IFNAMSIZ == 16
                )[20:24])
    except IOError:
        raise NetError("Error while retrieving IP address from interface %s." % ifname)


def get_host_ip_address(params):
    """
    returns ip address of host specified in host_ip_addr parameter If provided
    otherwise ip address on interface specified in netdst paramter is returned
    @param params
    """
    host_ip = params.get('host_ip_addr', None)
    if not host_ip:
        host_ip = get_ip_address_by_interface(params.get('netdst'))
        logging.warning("No IP address of host was provided, using IP address"
                        " on %s interface", str(params.get('netdst')))
    return host_ip


def get_linux_ifname(session, mac_address):
    """
    Get the interface name through the mac address.

    @param session: session to the virtual machine
    @mac_address: the macaddress of nic

    @raise error.TestError in case it was not possible to determine the
            interface name.
    """
    def ifconfig_method():
        try:
            output = session.cmd("ifconfig -a")
            return re.findall("(\w+)\s+Link.*%s" % mac_address, output,
                              re.IGNORECASE)[0]
        except IndexError:
            return None

    def iplink_method():
        try:
            output = session.cmd("ip link | grep -B1 '%s' -i" % mac_address)
            return re.findall("\d+:\s+(\w+):\s+.*", output, re.IGNORECASE)[0]
        except (aexpect.ShellCmdError, IndexError):
            return None

    def sys_method():
        try:
            interfaces = session.cmd('ls --color=never /sys/class/net')
        except error.CmdError, e:
            logging.debug("Error listing /sys/class/net: %s" % e)
            return None

        interfaces = interfaces.strip()
        for interface in interfaces.split(" "):
            if interface:
                try:
                    i_cmd = "cat /sys/class/net/%s/address" % interface
                    mac_address_interface = session.cmd(i_cmd)
                    mac_address_interface = mac_address_interface.strip()
                    if mac_address_interface == mac_address:
                        return interface
                except aexpect.ShellCmdError:
                    pass

        # If after iterating through interfaces (or no interfaces found)
        # no interface name was found, just return None
        return None

    # Try ifconfig first
    i = ifconfig_method()
    if i is not None:
        return i

    # No luck, try ip link
    i = iplink_method()
    if i is not None:
        return i

    # No luck, look on /sys
    i = sys_method()
    if i is not None:
        return i

    # If we came empty handed, let's raise an error
    raise error.TestError("Failed to determine interface name with "
                          "mac %s" % mac_address)


def restart_guest_network(session, nic_name=None):
    """
    Restart guest's network via serial console.

    @param session: session to virtual machine
    @nic_name: nic card name in guest to restart
    """
    if_list = []
    if not nic_name:
        # initiate all interfaces on guest.
        o = session.cmd_output("ip link")
        if_list = re.findall(r"\d+: (eth\d+):", o)
    else:
        if_list.append(nic_name)

    if if_list:
        session.sendline("killall dhclient && "
                         "dhclient %s &" % ' '.join(if_list))


def update_mac_ip_address(vm, params, timeout=None):
    """
    Get mac and ip address from guest then update the mac pool and
    address cache

    @param vm: VM object
    @param params: Dictionary with the test parameters.
    """
    network_query = params.get("network_query", "ifconfig")
    restart_network = params.get("restart_network", "service network restart")
    mac_ip_filter = params.get("mac_ip_filter")
    if timeout is None:
        timeout = int(params.get("login_timeout"))
    session = vm.wait_for_serial_login(timeout=360)
    end_time = time.time() + timeout
    macs_ips = []
    i = 0
    while time.time() < end_time:
        try:
            if i % 3 == 0:
                session.cmd(restart_network)
            o = session.cmd_status_output(network_query)[1]
            macs_ips = re.findall(mac_ip_filter, o)
            # Get nics number
        except Exception, e:
            logging.warn(e)
        nics =  params.get("nics")
        nic_minimum = len(re.split("\s+", nics.strip()))
        if len(macs_ips) == nic_minimum:
            break
        i += 1
        time.sleep(5)
    if len(macs_ips) < nic_minimum:
        logging.warn("Not all nics get ip address")

    for (mac, ip) in macs_ips:
        vlan = macs_ips.index((mac, ip))
        if "-" in mac:
            mac = mac.replace("-", ".")
        vm.address_cache[mac.lower()] = ip
        vm.virtnet.set_mac_address(vlan, mac)
