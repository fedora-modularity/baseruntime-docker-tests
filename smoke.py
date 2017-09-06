#!/usr/bin/env python

import os
import subprocess
import re
import shutil
import stat
import tarfile
import tempfile

from avocado import main
from moduleframework import module_framework

import brtconfig


class BaseRuntimeSmokeTest(module_framework.AvocadoTest):
    """
    :avocado: enable
    """

    def setUp(self):
        super(self.__class__, self).setUp()
        self.compiler_resource_dir = brtconfig.get_compiler_test_dir(self)
        self.compiler_test_dir = None
        self.start()

    def _check_cmd_result(self, cmd, return_code, cmd_output, expect_pass=True):
        """
        Check based on return code if command passed or failed as expected
        """
        if return_code == 0 and expect_pass:
            self.log.info("command '%s' succeeded with output:\n%s" %
                          (cmd, cmd_output))
            return True
        elif return_code != 0 and not expect_pass:
            self.log.info("command '%s' failed as expected with output:\n%s" %
                          (cmd, cmd_output))
            return True
        self.error("command '%s' returned unexpected exit status %d; output:\n%s" %
                       (cmd, return_code, cmd_output))
        return False

    def testSmoke(self):
        """
        Run several smoke tests
        """

        # TODO: fill this "placeholder" with actual, complete, smoke tests:

        smoke_pass = [
            "echo 'Hello, World!'",
            "cat /etc/redhat-release",
            "rpm -q glibc"]

        smoke_fail = [
            "exit 1"]

        for cmd in smoke_pass:
            cmd_result = self.run("%s" % cmd, ignore_status=True)
            cmd_output = cmd_result.stdout + cmd_result.stderr
            self._check_cmd_result(cmd, cmd_result.exit_status, cmd_output)

        for cmd in smoke_fail:
            cmd_result = self.run("%s" % cmd, ignore_status=True)
            cmd_output = cmd_result.stdout + cmd_result.stderr
            self._check_cmd_result(cmd, cmd_result.exit_status, cmd_output, expect_pass=False)


    def _get_all_installed_pkgs(self):
        try:
            cmd_result = self.run("rpm -qa --qf='%{{name}}\n'")
        except BaseException as details:
            self.error("Could not get all installed packages (%s)" % details)
        except:
            self.error("Could not get all installed packages")
        output_list = cmd_result.stdout.split("\n")
        #remove empty string from the list
        return [item for item in output_list if item]

    def testRequiredPackages(self):
        """
        Check if all required packages defined on yaml file are installed
        """

        profile_name = brtconfig.get_test_profile(self)
        mod_yaml = self.getModulemdYamlconfig()
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
            self.error("No rpm is defined for container")

        installed_pkgs = self._get_all_installed_pkgs()

        for req_pkg in req_pkgs:
            if req_pkg not in installed_pkgs:
                self.error("Required package '%s' is not installed" % req_pkg)

    def testInstalledPackages(self):
        """
        Check if only the expected packages are installed on module
        """

        expected_pkgs = None

        if not self.moduleType:
            self.error("moduleType is not defined")

        all_installed_pkgs_path = ("resources/installed_packages/all_installed_pkgs_%s.txt"
                                   % self.moduleType)
        try:
            with open(all_installed_pkgs_path) as f:
                expected_pkgs = f.read().splitlines()
        except:
            self.error("Could not read the expected installed packages list")

        if not expected_pkgs:
            self.error("List of expected installed packages is empty")

        installed_pkgs = self._get_all_installed_pkgs()
        if not installed_pkgs:
            self.error("It seems there is no package installed in the module")

        for pkg in installed_pkgs:
            if pkg not in expected_pkgs:
                self.error("Did not expect to have package '%s' installed" % pkg)

    def testInstallAllPackages(self):
        """
        Check if all packages that we ship are able to be installed on module
        """

        profile_name = brtconfig.get_test_profile(self)
        mod_yaml = self.getModulemdYamlconfig()
        if not mod_yaml:
            self.error("Could not read modulemd Yaml file")

        if "data" not in mod_yaml.keys():
            self.error("'data' key was not found in modulemd Yaml file")

        if "api" not in mod_yaml["data"].keys():
            self.error("'api' key was not found in 'data' section")

        if "rpms" not in mod_yaml["data"]["api"].keys():
            self.error("'rpms' key was not found in 'api'")

        all_api_pkgs = mod_yaml["data"]["api"]["rpms"]

        repo_path = None
        mod_dep = self.getModuleDependencies()
        mod_name = "baseruntime"
        if mod_dep and mod_name in mod_dep.keys():
            if "urls" in mod_dep[mod_name].keys():
                if len(mod_dep[mod_name]["urls"]) != 1:
                    self.error("Expected exactly 1 repo url for %s" % mod_name)
            repo_path = mod_dep[mod_name]["urls"][0]


        # docker uses the repo defined on mock cfg
        if self.moduleType == "docker":
            mockcfg = ""
            with open(brtconfig.get_mockcfg(self)) as f:
                mockcfg = f.read()
            for line in mockcfg.split("\n"):
                m = re.match("baseurl=(\S+)", line)
                if m:
                    repo_path = m.group(1)

        if not repo_path:
            self.error("Could not find repo to query the packages")

        #Query all available packages in our repo
        query_repo_cmd = "repoquery  -a --qf '%%{{name}}' --repofrompath=0,%s --repoid=0" % repo_path
        all_repo_pkgs = self.runHost(query_repo_cmd).stdout.split("\n")

        all_avail_pkgs = []
        #Available packages are the ones from API that are available on repo
        for pkg in all_api_pkgs:
            if pkg in all_repo_pkgs:
                all_avail_pkgs.append(pkg)

        skip_pkg_image = {}
        skip_pkg_image["docker"] = ["kernel", "dracut"]
        skip_pkg_image["nspawn"] = ["kernel", "dracut"]
        conflict_pkgs = {
            "coreutils" : "coreutils-single",
            "libcrypt-nss" : "libcrypt"
        }

       #Try to install packages that have conflicting packages installed
        for pkg in conflict_pkgs.keys():
            self.run("microdnf remove %s" % conflict_pkgs[pkg])
            self.run("rpm -q %s" % pkg)
            self.run("microdnf remove %s" % pkg)
            self.run("rpm -q %s" % conflict_pkgs[pkg])

        pkgs_2_install = []
        for pkg in all_avail_pkgs:
            if pkg in conflict_pkgs.keys():
                continue
            #Do not install packages such as dracut* on docker.
            skip = False
            for skip_pkg in skip_pkg_image[self.moduleType]:
                if pkg.startswith(skip_pkg):
                    skip = True
            if not skip:
                pkgs_2_install.append(pkg)
        self.run("microdnf install %s > /dev/null" % " ".join(pkgs_2_install))


    def testUserManipulation(self):
        """
        Check if can add, remove and modify user
        """

        #We want to run multiple commands using same docker container
        new_user = "usertest"
        pass_cmds = []
        #Create new user
        pass_cmds.append("adduser %s" % new_user)
        #Make sure user is created
        pass_cmds.append("cat /etc/passwd | grep %s" % new_user)
        pass_cmds.append("ls /home/%s" % new_user)
        #set user password
        pass_cmds.append("usermod --password testpassword %s" % new_user)
        #Test new user functionality
        pass_cmds.append('su - %s -c "touch ~/testfile.txt"' % new_user)
        #Make sure the file was created by the correct user
        pass_cmds.append("ls -allh /home/%s/testfile.txt | grep '%s %s'" %
                         (new_user, new_user, new_user))
        #Remove user
        pass_cmds.append("userdel -r %s" % new_user)
        for cmd in pass_cmds:
            cmd_result = self.run("%s" % cmd, ignore_status=True)
            cmd_output = cmd_result.stdout + cmd_result.stderr
            self._check_cmd_result(cmd, cmd_result.exit_status, cmd_output)

        fail_cmds = []
        #Make sure user is removed
        fail_cmds.append("ls /home/%s" % new_user)
        fail_cmds.append("cat /etc/passwd | grep usertest")
        #relying on __del__ from BaseRuntimeRunCmd to remove container
        for cmd in fail_cmds:
            cmd_result = self.run("%s" % cmd, ignore_status=True)
            cmd_output = cmd_result.stdout + cmd_result.stderr
            self._check_cmd_result(cmd, cmd_result.exit_status, cmd_output, expect_pass=False)

    def testOsRelease(self):
        """
        Check if OS release information is correct
        """

        test_path = "resources/os_release/os_release.sh"
        dest_path = "/tmp/os_release.sh"
        try:
            self.copyTo(test_path, dest_path)
        except:
            self.error("Could not copy test file from %s to module %s" %
                       (test_path, dest_path))

        try:
            self.run(dest_path)
        except:
            self.error("%s failed" % dest_path)

        try:
            self.run("rm -f %s" % dest_path)
        except:
            self.error("Could not delete %s" % dest_path)

    def test_glibc_i18n(self):
        """
        Test glibc support to internationalization
        """

        lang_default = {
            #cmd : cmd_output
            "ls /invalid_path" : "ls: cannot access '/invalid_path': No such file or directory",
            "cp invalid_file tmp" : "cp: cannot stat 'invalid_file': No such file or directory",
            "date -u -d \"2017-03-31\"" : "Fri Mar 31 00:00:00 UTC 2017",
            "touch file; yes | rm -i file" : "rm: remove regular empty file 'file'?",
            "numfmt --grouping 1234567890.98" : "1234567890.98"
        }

        lang_english = {
            "LC_ALL=en_US ls /invalid_path" : "ls: cannot access '/invalid_path': No such file or directory",
            "LC_ALL=en_US cp invalid_file tmp" : "cp: cannot stat 'invalid_file': No such file or directory",
            "LC_ALL=en_US date -u -d \"2017-03-31\"" : "Fri Mar 31 00:00:00 UTC 2017",
            "touch file; yes | LC_ALL=en_US rm -i file" : "rm: remove regular empty file 'file'?",
            "LC_ALL=en_US numfmt --grouping 1234567890.98" : "1,234,567,890.98"
        }

        lang_spanish = {
            "LC_ALL=es_ES ls /invalid_path" : "No existe el fichero o el directorio",
            "LC_ALL=es_ES cp invalid_file tmp" : "No existe el fichero o el directorio",
            "LC_ALL=es_ES date -u -d \"2017-03-31\"" : "vie mar 31 00:00:00 UTC 2017",
            "LC_ALL=es_ES numfmt --grouping 1234567890,98" : "1.234.567.890,98"
        }

        langs = {}
        langs["default"] = {
            "pkg" : "glibc-minimal-langpack",
            "cmds" : lang_default
        }

        langs["english"] = {
            "pkg" : "glibc-langpack-en",
            "cmds" : lang_english
        }

        langs["spanish"] = {
            "pkg" : "glibc-langpack-es",
            "cmds" : lang_spanish
        }

        #Check if C.utf8 is the default locale
        self.run("echo $LANG | grep 'C.utf8'")

        for i18n in langs.keys():
            lang = langs[i18n]
            self.log.info("Testing %s" % lang["pkg"])

            install_package = True
            # glibc-minimal-langpack is installed by default
            if lang["pkg"] == "glibc-minimal-langpack":
                install_package = False

            if install_package:
                try:
                    self.run("microdnf install %s" % lang["pkg"])
                except:
                    self.error("Could not install %s" % lang["pkg"])

            for cmd in lang["cmds"].keys():
                cmd_result = self.run("%s" % cmd, ignore_status=True)
                output = cmd_result.stdout
                output += cmd_result.stderr
                output = output.strip()
                #search for pattern as Spanish might have special characters
                if not re.search(lang["cmds"][cmd], output):
                    self.error("'%s'expected output '%s', but got '%s'" %
                               (cmd, lang["cmds"][cmd], output))

            if install_package:
                try:
                    self.run("microdnf remove %s" % lang["pkg"])
                except:
                    self.error("Could not remove %s" % lang["pkg"])

    def test_dnf(self):
        """
        Check if DNF is able to install a package
        """
        package = "tar"
        self.run("microdnf install dnf")
        self.run("dnf install -y %s" % package)
        self.run("dnf remove -y %s" % package)
        self.run("microdnf remove dnf")

    def _prepare_compiler_test_directory(self):

        # create a temporary directory
        tmpdir = tempfile.mkdtemp()

        self.log.info("Compiler test temporary directory is %s" % tmpdir)

        # Copy the `hello.sh` script from this resource directory into the
        # temporary directory
        src = os.path.join(self.compiler_resource_dir, "hello.sh")
        dest = os.path.join(tmpdir, "hello.sh")
        try:
            shutil.copy(src, dest)
        except shutil.Error as e:
            self.log.info('Error: %s' % e)
        except IOError as e:
            self.log.info('Error: %s' % e.strerror)

        # make sure destination script is executable
        st = os.stat(dest)
        os.chmod(dest, st.st_mode | stat.S_IEXEC)

        # Place a gzipped tarball of `hello.c` and `Makefile` from the
        # resource directory into the temporary directory with the name
        # `hello.tgz`.
        dest = os.path.join(tmpdir, "hello.tgz")
        tar = tarfile.open(dest, "w:gz")
        for f in ["hello.c", "Makefile"]:
            src = os.path.join(self.compiler_resource_dir, f)
            tar.add(src, arcname=f)
        tar.close()

        self.compiler_test_dir = tmpdir

    def _cleanup_compiler_test_directory(self):

        # clean up the temporary directory
        if self.compiler_test_dir:
            self.log.info("cleaning up compiler test directory")
            shutil.rmtree(self.compiler_test_dir, ignore_errors=True)

    def testCompiler(self):
        """
        Run a basic C compiler test on our docker image.

        This actually tests the integration of several things, including the
        ability to install packages, extract a gzipped tarball, run make to
        compile a very simple C program, and run the compiled executable.
        """

        self._prepare_compiler_test_directory()

        #The test dir should be the same one used on hello.sh
        mod_compiler_test_dir = "/mnt"

        #Make sure there is a container running
        #TODO: Remove start() once https://pagure.io/modularity-testing-framework/issue/8 is fixed
        self.start()

        try:
            self.copyTo("%s/." % self.compiler_test_dir, mod_compiler_test_dir)
        except:
            self.error("Could not copy test files from %s to module %s" %
                       (self.compiler_test_dir, mod_compiler_test_dir))

        cmdline = "%s/hello.sh" % mod_compiler_test_dir
        cmd_result = self.run("%s" % cmdline, ignore_status=True)
        test_stdout = cmd_result.stdout
        test_stderr = cmd_result.stderr
        if cmd_result.exit_status:
            self.error("command '%s' returned exit status %d; output:\n%s\nstderr:\n%s" %
                       (cmdline, cmd_result.exit_status, test_stdout, test_stderr))

        self.log.info("command '%s' succeeded with output:\n%s\nstderr:\n%s" %
                      (cmdline, test_stdout, test_stderr))

        # make sure we get exactly what we expect on stdout
        # (all other output from commands in the script were sent to stderr)
        expected_stdout = 'Hello, world!\n'
        self.log.info("checking that compiler test returned expected output: %s" %
                      repr(expected_stdout))
        if test_stdout != expected_stdout:
            self.error("compiler test did not return unexpected output: %s" %
                       repr(test_stdout))

    def tearDown(self):
        """
        Tear-down
        """
        super(self.__class__, self).tearDown()

        self._cleanup_compiler_test_directory()

if __name__ == "__main__":
    main()
