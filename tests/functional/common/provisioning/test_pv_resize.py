import ddt
from cnslibs.common.cns_libs import (
    enable_pvc_resize)
from cnslibs.common import heketi_ops
from cnslibs.common.openshift_ops import (
    resize_pvc,
    get_pod_name_from_dc,
    get_pv_name_from_pvc,
    oc_create_app_dc_with_io,
    oc_delete,
    oc_rsh,
    scale_dc_pod_amount_and_wait,
    verify_pv_size,
    verify_pvc_size,
    wait_for_events,
    wait_for_pod_be_ready,
    wait_for_resource_absence)
from cnslibs.common.openshift_version import get_openshift_version
from cnslibs.common.baseclass import BaseClass
from cnslibs.common.exceptions import ExecutionError
from glusto.core import Glusto as g


@ddt.ddt
class TestPvResizeClass(BaseClass):
    """Test cases for PV resize"""

    @classmethod
    def setUpClass(cls):
        super(TestPvResizeClass, cls).setUpClass()
        cls.node = cls.ocp_master_node[0]
        if get_openshift_version() < "3.9":
            cls.skip_me = True
            return
        enable_pvc_resize(cls.node)

    def setUp(self):
        super(TestPvResizeClass, self).setUp()
        if getattr(self, "skip_me", False):
            msg = ("pv resize is not available in openshift "
                   "version %s " % self.version)
            g.log.error(msg)
            raise self.skipTest(msg)

    @ddt.data(False, True)
    def test_pv_resize_with_prefix_for_name(self,
                                            create_vol_name_prefix=False):
        """Validate PV resize with and without name prefix"""
        dir_path = "/mnt/"
        node = self.ocp_client[0]

        # Create PVC
        self.create_storage_class(
            allow_volume_expansion=True,
            create_vol_name_prefix=create_vol_name_prefix)
        pvc_name = self.create_and_wait_for_pvc()

        # Create DC with POD and attached PVC to it.
        dc_name = oc_create_app_dc_with_io(node, pvc_name)
        self.addCleanup(oc_delete, node, 'dc', dc_name)
        self.addCleanup(scale_dc_pod_amount_and_wait,
                        node, dc_name, 0)

        pod_name = get_pod_name_from_dc(node, dc_name)
        wait_for_pod_be_ready(node, pod_name)
        if create_vol_name_prefix:
            ret = heketi_ops.verify_volume_name_prefix(
                node, self.sc['volumenameprefix'],
                self.sc['secretnamespace'],
                pvc_name, self.heketi_server_url)
            self.assertTrue(ret, "verify volnameprefix failed")
        cmd = ("dd if=/dev/urandom of=%sfile "
               "bs=100K count=1000") % dir_path
        ret, out, err = oc_rsh(node, pod_name, cmd)
        self.assertEqual(ret, 0, "failed to execute command %s on %s" % (
                             cmd, node))
        cmd = ("dd if=/dev/urandom of=%sfile2 "
               "bs=100K count=10000") % dir_path
        ret, out, err = oc_rsh(node, pod_name, cmd)
        self.assertNotEqual(ret, 0, " This IO did not fail as expected "
                            "command %s on %s" % (cmd, node))
        pvc_size = 2
        resize_pvc(node, pvc_name, pvc_size)
        verify_pvc_size(node, pvc_name, pvc_size)
        pv_name = get_pv_name_from_pvc(node, pvc_name)
        verify_pv_size(node, pv_name, pvc_size)
        oc_delete(node, 'pod', pod_name)
        wait_for_resource_absence(node, 'pod', pod_name)
        pod_name = get_pod_name_from_dc(node, dc_name)
        wait_for_pod_be_ready(node, pod_name)
        cmd = ("dd if=/dev/urandom of=%sfile_new "
               "bs=50K count=10000") % dir_path
        ret, out, err = oc_rsh(node, pod_name, cmd)
        self.assertEqual(ret, 0, "failed to execute command %s on %s" % (
                             cmd, node))

    def _pv_resize(self, exceed_free_space):
        dir_path = "/mnt"
        pvc_size_gb, min_free_space_gb = 1, 3

        # Get available free space disabling redundant devices and nodes
        heketi_url = self.heketi_server_url
        node_id_list = heketi_ops.heketi_node_list(
            self.heketi_client_node, heketi_url)
        self.assertTrue(node_id_list)
        nodes = {}
        min_free_space = min_free_space_gb * 1024**2
        for node_id in node_id_list:
            node_info = heketi_ops.heketi_node_info(
                self.heketi_client_node, heketi_url, node_id, json=True)
            if (node_info['state'].lower() != 'online' or
                    not node_info['devices']):
                continue
            if len(nodes) > 2:
                out = heketi_ops.heketi_node_disable(
                    self.heketi_client_node, heketi_url, node_id)
                self.assertTrue(out)
                self.addCleanup(
                    heketi_ops.heketi_node_enable,
                    self.heketi_client_node, heketi_url, node_id)
            for device in node_info['devices']:
                if device['state'].lower() != 'online':
                    continue
                free_space = device['storage']['free']
                if (node_id in nodes.keys() or free_space < min_free_space):
                    out = heketi_ops.heketi_device_disable(
                        self.heketi_client_node, heketi_url, device['id'])
                    self.assertTrue(out)
                    self.addCleanup(
                        heketi_ops.heketi_device_enable,
                        self.heketi_client_node, heketi_url, device['id'])
                    continue
                nodes[node_id] = free_space
        if len(nodes) < 3:
            raise self.skipTest(
                "Could not find 3 online nodes with, "
                "at least, 1 online device having free space "
                "bigger than %dGb." % min_free_space_gb)

        # Calculate maximum available size for PVC
        available_size_gb = int(min(nodes.values()) / (1024**2))

        # Create PVC
        self.create_storage_class(allow_volume_expansion=True)
        pvc_name = self.create_and_wait_for_pvc(pvc_size=pvc_size_gb)

        # Create DC with POD and attached PVC to it
        dc_name = oc_create_app_dc_with_io(self.node, pvc_name)
        self.addCleanup(oc_delete, self.node, 'dc', dc_name)
        self.addCleanup(scale_dc_pod_amount_and_wait, self.node, dc_name, 0)
        pod_name = get_pod_name_from_dc(self.node, dc_name)
        wait_for_pod_be_ready(self.node, pod_name)

        if exceed_free_space:
            # Try to expand existing PVC exceeding free space
            resize_pvc(self.node, pvc_name, available_size_gb)
            wait_for_events(self.node, obj_name=pvc_name,
                            event_reason='VolumeResizeFailed')

            # Check that app POD is up and runnig then try to write data
            wait_for_pod_be_ready(self.node, pod_name)
            cmd = (
                "dd if=/dev/urandom of=%s/autotest bs=100K count=1" % dir_path)
            ret, out, err = oc_rsh(self.node, pod_name, cmd)
            self.assertEqual(
                ret, 0,
                "Failed to write data after failed attempt to expand PVC.")
        else:
            # Expand existing PVC using all the available free space
            expand_size_gb = available_size_gb - pvc_size_gb
            resize_pvc(self.node, pvc_name, expand_size_gb)
            verify_pvc_size(self.node, pvc_name, expand_size_gb)
            pv_name = get_pv_name_from_pvc(self.node, pvc_name)
            verify_pv_size(self.node, pv_name, expand_size_gb)
            wait_for_events(
                self.node, obj_name=pvc_name,
                event_reason='VolumeResizeSuccessful')

            # Recreate app POD
            oc_delete(self.node, 'pod', pod_name)
            wait_for_resource_absence(self.node, 'pod', pod_name)
            pod_name = get_pod_name_from_dc(self.node, dc_name)
            wait_for_pod_be_ready(self.node, pod_name)

            # Write data on the expanded PVC
            cmd = ("dd if=/dev/urandom of=%s/autotest "
                   "bs=1M count=1025" % dir_path)
            ret, out, err = oc_rsh(self.node, pod_name, cmd)
            self.assertEqual(
                ret, 0, "Failed to write data on the expanded PVC")

    def test_pv_resize_no_free_space(self):
        """Validate PVC resize fails if there is no free space available"""
        self._pv_resize(exceed_free_space=True)

    def test_pv_resize_by_exact_free_space(self):
        """Validate PVC resize when resized by exact available free space"""
        self._pv_resize(exceed_free_space=False)

    def test_pv_resize_try_shrink_pv_size(self):
        """Validate whether reducing the PV size is allowed"""
        dir_path = "/mnt/"
        node = self.ocp_master_node[0]

        # Create PVC
        pv_size = 5
        self.create_storage_class(allow_volume_expansion=True)
        pvc_name = self.create_and_wait_for_pvc(pvc_size=pv_size)

        # Create DC with POD and attached PVC to it.
        dc_name = oc_create_app_dc_with_io(node, pvc_name)
        self.addCleanup(oc_delete, node, 'dc', dc_name)
        self.addCleanup(scale_dc_pod_amount_and_wait,
                        node, dc_name, 0)

        pod_name = get_pod_name_from_dc(node, dc_name)
        wait_for_pod_be_ready(node, pod_name)

        cmd = ("dd if=/dev/urandom of=%sfile "
               "bs=100K count=3000") % dir_path
        ret, out, err = oc_rsh(node, pod_name, cmd)
        self.assertEqual(ret, 0, "failed to execute command %s on %s" % (
                             cmd, node))
        pvc_resize = 2
        with self.assertRaises(ExecutionError):
            resize_pvc(node, pvc_name, pvc_resize)
        verify_pvc_size(node, pvc_name, pv_size)
        pv_name = get_pv_name_from_pvc(node, pvc_name)
        verify_pv_size(node, pv_name, pv_size)
        cmd = ("dd if=/dev/urandom of=%sfile_new "
               "bs=100K count=2000") % dir_path
        ret, out, err = oc_rsh(node, pod_name, cmd)
        self.assertEqual(ret, 0, "failed to execute command %s on %s" % (
                             cmd, node))
