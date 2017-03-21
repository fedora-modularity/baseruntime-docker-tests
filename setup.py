#!/usr/bin/env python

import os
import subprocess
import re
import sys
import configparser
import tempfile

from avocado import main
from avocado import Test

import cleanup
import brtconfig


class BaseRuntimeSetupDocker(Test):

    def setUp(self):

        mockcfg = brtconfig.get_mockcfg(self)
        self.mockcfg = mockcfg

        mock_root = ''
        with open(mockcfg, 'r') as mock_cfgfile:
            for line in mock_cfgfile:
                if re.match("config_opts\s*\[\s*'root'\s*\]", line) is not None:
                    mock_root = line.split('=')[1].split("'")[1]
        if len(mock_root) == 0:
            self.error("mock configuration file %s does not specify mock root" %
                mockcfg)
        self.log.info("mock root: %s" % mock_root)
        self.mock_root = mock_root

        self.br_image_name = brtconfig.get_docker_image_name(self)

    def _run_command(self, cmd):
        try:
            cmd_output = subprocess.check_output(
                cmd, stderr=subprocess.STDOUT, shell=True)
        except subprocess.CalledProcessError as e:
            self.error("command '%s' returned exit status %d; output:\n%s" %
                       (e.cmd, e.returncode, e.output))
        else:
            self.log.info("command  '%s' succeeded with output:\n%s" %
                          (cmd, cmd_output))

    def _configure_mock_microdnf(self):
        """
        Configure mock chroot for microdnf so it carrys into the docker image
        """

        # fetch the dnf.conf file from the mock chroot that was conveniently
        # created based on the yum.conf value in the mock configuration file
        tmpdnfcfg = tempfile.NamedTemporaryFile(delete=False)
        self._run_command('mock -r %s --copyout /etc/dnf/dnf.conf %s' %
                          (self.mockcfg, tmpdnfcfg.name))

        with open(tmpdnfcfg.name, 'r') as dnffile:
            contents = dnffile.read()
        self.log.info(
            "Contents of original dnf.conf generated by mock:\n%s" % contents)

        # load the dnf.conf file and remove the [main] section so only the
        # repo section(s) remain
        config = configparser.ConfigParser()
        config.read(tmpdnfcfg.name)
        self.log.info("Found the following configuration section(s): %s" %
                      ' '.join(config.sections()))
        if 'main' in config:
            del config['main']

        # write out the cleaned up repo configuration
        tmpyumcfg = tempfile.NamedTemporaryFile(delete=False)
        with open(tmpyumcfg.name, 'w') as repofile:
            config.write(repofile, space_around_delimiters=False)

        with open(tmpyumcfg.name, 'r') as yumfile:
            contents = yumfile.read()
        self.log.info("Contents of revised yum repo config:\n%s" % contents)

        # copy the new yum repo configuration file into the mock chroot
        self._run_command(
            'mock -r %s --copyin %s /etc/yum.repos.d/build.repo' % (self.mockcfg, tmpyumcfg.name))
        self._run_command(
            'mock -r %s --chroot "chmod 644 /etc/yum.repos.d/build.repo"' % self.mockcfg)

        # remove the temporary files
        os.remove(tmpdnfcfg.name)
        os.remove(tmpyumcfg.name)

        # /etc/pki/rpm-gpg directory must exist or microdnf will explode
        self._run_command(
            'mock -r %s --chroot "mkdir -p -m=755 /etc/pki/rpm-gpg"' % self.mockcfg)

    def testCreateDockerImage(self):

        # Clean-up any old test artifacts (docker containers, image, mock root)
        # first:
        try:
            cleanup.cleanup_docker_and_mock(self.mockcfg, self.br_image_name)
        except:
            self.error("artifact cleanup failed")
        else:
            self.log.info("artifact cleanup successful")

        # Initialize chroot with mock
        self._run_command('mock -r %s --init' % self.mockcfg)

        # Configure mock chroot for microdnf so it carrys into the docker image
        self._configure_mock_microdnf()

        # check if "sudo" allows us to tar up the chroot without a password
        # Note: this must be configured in "sudoers" to work!
        tar_cmd = "tar -C /var/lib/mock/%s/root -c ." % self.mock_root
        try:
            cmd_output = subprocess.check_output(
                "sudo -n %s >/dev/null" % tar_cmd,
                stderr=subprocess.STDOUT, shell=True)
        except subprocess.CalledProcessError as e:
            # no luck using "sudo", warn and proceed as ordinary user without
            # it
            self.log.info("command '%s' returned exit status %d; output:\n%s" %
                          (e.cmd, e.returncode, e.output))
            self.log.warning("NO SUDO RIGHTS TO RUN COMMAND '%s' AS ROOT" %
                             tar_cmd)
            self.log.warning("GENERATED DOCKER IMAGE '%s' MAY BE INCOMPLETE!" %
                             self.br_image_name)
        else:
            # "sudo" works, so use it
            tar_cmd = "sudo -n " + tar_cmd

        # Import mock chroot as a docker image
        self._run_command("%s | docker import - %s" %
                          (tar_cmd, self.br_image_name))

if __name__ == "__main__":
    main()
