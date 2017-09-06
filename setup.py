#!/usr/bin/env python

import os
import subprocess
import re
import sys
import configparser
import tempfile

from avocado import main
from avocado import Test
from moduleframework import module_framework

import cleanup
import brtconfig


class BaseRuntimeSetupDocker(Test):

    def setUp(self):

        self.mtf = module_framework.CommonFunctions()
        self.mockcfg = brtconfig.get_mockcfg(self)
        self.br_image_name = brtconfig.get_docker_image_name(self)

    def _process_mockcfg(self):

        profile_name = brtconfig.get_test_profile(self)
        mockcfg = self.mockcfg

        mock_root = ''
        mockcfg_lines = []
        #Regex to get packages that are configured on mockcfg to be installed
        chroot_setup_pkg_regex = re.compile("config_opts\s*\[\s*'chroot_setup_cmd'\s*\]\s*="
                                            "\s*'install --setopt=tsflags=nodocs\s*(.*)\s*'")
        chroot_setup_pkgs = None
        with open(mockcfg, 'r') as mock_cfgfile:
            found_setup_cmd = False
            for line in mock_cfgfile:
                mockcfg_lines.append(line)
                if re.match("config_opts\s*\[\s*'root'\s*\]", line) is not None:
                    mock_root = line.split('=')[1].split("'")[1]
                if re.match("config_opts\s*\[\s*'chroot_setup_cmd'\s*\]", line) is not None:
                    found_setup_cmd = True
                    #Check if there are packages defined on chroot_setup_cmd
                    m = chroot_setup_pkg_regex.match(line)
                    if m:
                        chroot_setup_pkgs = sorted(m.group(1).split())
        if len(mock_root) == 0:
            self.error("mock configuration file %s does not specify mock root" %
                mockcfg)
        self.log.info("mock root: %s" % mock_root)
        self.mock_root = mock_root

        if not found_setup_cmd:
            self.error("mock configuration file %s does not define chroot_setup_cmd" % mockcfg)

        #Need to get all packages that need to be installed
        mod_yaml = self.mtf.getModulemdYamlconfig()
        if not mod_yaml:
            self.error("Could not read modulemd Yaml file")

        if "data" not in mod_yaml.keys():
            self.error("'data' key was not found in modulemd Yaml file")

        if "profiles" not in mod_yaml["data"].keys():
            self.error("'profiles' key was not found in 'data' section")

        if profile_name not in mod_yaml["data"]["profiles"].keys():
            self.error("'%s' key was not found in 'profiles' section" % profile_name)

        base_profile = mod_yaml["data"]["profiles"][profile_name]
        if "rpms" not in base_profile.keys():
            self.error("'rpms' key was not found in '%s' profile" % profile_name)

        req_pkgs = base_profile["rpms"]
        if not req_pkgs:
            self.error("Could not find any package to be installed in the image")

        #Only update mockcfg if the list of packages changed
        if cmp(chroot_setup_pkgs, sorted(req_pkgs)):
            #Need to change chroot_setup_cmd line on mockcfg file
            setup_cmd = "install --setopt=tsflags=nodocs "
            setup_cmd += " ".join(req_pkgs)
            with open(mockcfg, 'w') as mock_cfgfile:
                for line in mockcfg_lines:
                    if re.match("config_opts\s*\[\s*'chroot_setup_cmd'\s*\]", line) is not None:
                        line = "config_opts['chroot_setup_cmd'] = '%s'\n" % setup_cmd
                    mock_cfgfile.write(line)

            #Test will exit with WARN to inform the config file has changed
            self.log.warning("List of packages to be installed by mock changed")

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

    def _set_dnf_conf(self):
        filename = "/etc/dnf/dnf.conf"
        path = "/var/lib/mock/" + self.mock_root + "/root" + filename

        conf = "EOF\n"
        conf += "[main]\n"
        conf += "gpgcheck=1\n"
        conf += "installonly_limit=3\n"
        conf += "clean_requirements_on_remove=True\n"
        conf += "EOF\n"

        cmd = "sudo tee %s << %s" % (path, conf)
        self._run_command(cmd)


    def testCreateDockerImage(self):

        self._process_mockcfg()

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

        self._set_dnf_conf()

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

        img_scratch = "%s-scratch" % self.br_image_name
        # Import mock chroot as a docker image
        self._run_command("%s | docker import - %s" %
                          (tar_cmd, img_scratch))

        docker_labels = brtconfig.get_docker_labels(self)
        #Dockerfile to use when building final image
        dockerfile = 'EOF\n'
        dockerfile += 'FROM %s\n' % img_scratch
        #Set default locale to C.utf8
        dockerfile += 'ENV LANG C.utf8\n'
        if docker_labels:
            for key in docker_labels.keys():
                dockerfile += 'LABEL %s="%s"\n' % (key, docker_labels[key])
        dockerfile += 'EOF\n'

        # Build final image with extra information from dockerfile
        self._run_command("docker build -t %s - << %s" %
                          (self.br_image_name, dockerfile))
        #Remove temporary image
        self._run_command("docker rmi %s" % img_scratch)

if __name__ == "__main__":
    main()
