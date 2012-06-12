import logging


### Configuration ###

# Minimum log level
#
# Recommended setting: logging.INFO
#
config_log_level = logging.INFO

# If true, ensures that all public headers are stored in the framework under
# the same directory hierarchy as they were in the source tree.
#
# Xcode by default places all headers at the same top level, but every other
# build tool  in the known universe preserves directory structure. For simple
# libraries it doesn't really matter much, but for ports of existing software
# packages or for bigger libraries, it makes sense to have more structure.
#
# Recommended setting: True
#
config_deep_header_hierarchy = True

# Specify where the top of the public header hierarchy is. This path is
# relative to the project's dir (PROJECT_DIR). You can reference environment
# variables using templating syntax (e.g. "${TARGET_NAME}/Some/Subdir")
#
# NOTE: Only used if config_deep_header_hierarchy is True.
#
# If this is set to None, the script will attempt to figure out for itself
# where the top of the header hierarchy is by looking for common path prefixes
# in the public header files. This process can fail if:
# - You only have one public header file.
# - Your source header files don't all have a common root.
#
# A common approach is to use "${TARGET_NAME}", working under the assumption
# that all of your header files share the common root of a directory under
# your project with the same name as your target (which is the Xcode default).
#
# Recommended setting: "${TARGET_NAME}"
#
config_deep_header_top = "${TARGET_NAME}"

# Warn when "DerivedData" is detected in any of the header, library, or
# framework search paths. In almost all cases, references to directories under
# DerivedData are added as a result of an Xcode bug and must be manually
# removed.
#
# Recommended setting: True
#
config_warn_derived_data = True

# Warn if no headers were marked public in this framework.
#
# Recommended setting: True
#
config_warn_no_public_headers = True

# Cause the build to fail if any warnings are issued.
#
# Recommended setting: True
#
config_fail_on_warnings = True



##############################################################################
#
# Don't touch anything below here unless you know what you're doing.
#
##############################################################################

import json
import os
import shlex
import shutil
import string
import subprocess
import sys
import time
import traceback
import collections
import re


# Globals

log = logging.getLogger('UFW')

issued_warnings = False


# Maintains the inter-build state of a project.
#
# One copy of the build state file is kept in the "Objects" dir for each
# platform. This ensures that cleaning any of the platforms will invalidate
# the build state.
#
class BuildState:

    def __init__(self):
        self.platforms = os.environ['SUPPORTED_PLATFORMS'].split(' ')
        self.reload()

    def reset(self):
        self.platforms = os.environ['SUPPORTED_PLATFORMS'].split(' ')
        self.last_completion = 0
        self.slave_platform = None
        self.slave_architectures = []
        self.slave_linked_archive_paths = []
        self.slave_built_fw_path = None
        self.slave_built_embedded_fw_path = None

    def set_slave_properties(self, architectures,
                             linked_archive_paths,
                             built_fw_path,
                             built_embedded_fw_path):
        self.slave_platform = os.environ['PLATFORM_NAME']
        self.slave_architectures = architectures
        self.slave_linked_archive_paths = linked_archive_paths
        self.slave_built_fw_path = built_fw_path
        self.slave_built_embedded_fw_path = built_embedded_fw_path

    def get_platform_path(self, platform):
        return "%s/%s-%s/%s.build/Objects-%s/ufw_build_state.json" % (os.environ['PROJECT_TEMP_DIR'],
                os.environ['CONFIGURATION'],
                platform,
                os.environ['PRODUCT_NAME'],
                os.environ['CURRENT_VARIANT'])

    def persist(self):
        for platform in self.platforms:
            self.save_to_json(self.get_platform_path(platform))

    def reload(self):
        self.reset()
        dicts = [self.load_from_json(self.get_platform_path(platform)) for platform in self.platforms]
        # If dicts don't all agree or couldn't be loaded, start a fresh build state.
        if not dicts[1:] == dicts[:-1] or dicts[0] is None:
            log.debug("Data not found or corrupt. Resetting")
            self.reset()
        else:
            self.__dict__ = dict(self.__dict__.items() + dicts[0].items())

    def load_from_json(self, filename):
        if os.path.exists(filename):
            with open(filename, "r") as f:
                return json.loads(f.read())
        return None

    def save_to_json(self, filename):
        parent = os.path.dirname(filename)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(filename, "w") as f:
            f.write(json.dumps(self.__dict__))


# Holds information about the current project and build environment.
#
class Project:

    def __init__(self, filename):
        self.project_data = self.load_from_file(filename)
        self.target = filter(lambda x: x['name'] == os.environ['TARGET_NAME'], self.project_data['targets'])[0]
        self.public_headers = self.get_build_phase_files('PBXHeadersBuildPhase', lambda x: x.get('settings', False) and x['settings'].get('ATTRIBUTES', False) and 'Public' in x['settings']['ATTRIBUTES'])
        self.static_libraries = self.get_build_phase_files('PBXFrameworksBuildPhase', lambda x: x['fileRef']['fileType'] == 'archive.ar' and x['fileRef']['sourceTree'] not in ['DEVELOPER_DIR', 'SDKROOT'])
        self.static_frameworks = self.get_build_phase_files('PBXFrameworksBuildPhase', lambda x: x['fileRef']['fileType'] == 'wrapper.framework' and x['fileRef']['sourceTree'] not in ['DEVELOPER_DIR', 'SDKROOT'])
        self.compilable_sources = self.get_build_phase_files('PBXSourcesBuildPhase', lambda x: x['fileRef']['fileType'].startswith('sourcecode.c.'))
        self.header_paths = [x['fullPath'] for x in self.public_headers]

        self.build_state = None
        self.headers_dir = "%s/%s/Headers" % (os.environ['BUILT_PRODUCTS_DIR'], os.environ['CONTENTS_FOLDER_PATH'])
        self.libtool_path = "%s/usr/bin/libtool" % os.environ['DT_TOOLCHAIN_DIR']
        self.project_filename = "%s/%s" % (os.environ['PROJECT_FILE_PATH'], "project.pbxproj")
        self.local_exe_path = os.environ['BUILT_PRODUCTS_DIR'] + "/" + os.environ['EXECUTABLE_PATH']
        self.local_architectures = os.environ['ARCHS'].split(' ')
        self.local_built_fw_path = os.environ['BUILT_PRODUCTS_DIR'] + "/" + os.environ['WRAPPER_NAME']
        self.local_built_embedded_fw_path = os.path.splitext(self.local_built_fw_path)[0] + ".embeddedframework"
        self.local_linked_archive_paths = [self.get_linked_ufw_archive_path(arch) for arch in self.local_architectures]
        self.local_platform = os.environ['PLATFORM_NAME']
        other_platforms = os.environ['SUPPORTED_PLATFORMS'].split(' ')
        other_platforms.remove(self.local_platform)
        self.other_platform = other_platforms[0]

        sdk_name = os.environ['SDK_NAME']
        if not sdk_name.startswith(self.local_platform):
            raise Exception("%s didn't start with %s" % (sdk_name, self.local_platform))
        self.sdk_version = sdk_name[len(self.local_platform):]

    # Load an Xcode project file
    def load_from_file(self, filename):
        project_file = json.loads(subprocess.check_output(["plutil", "-convert", "json", "-o", "-", filename]))
        for obj in project_file['objects'].values():
            self.fix_keys(obj)
        self.unflatten_object(project_file['objects'], project_file)
        project_data = project_file['rootObject']
        self.build_full_paths(project_data['mainGroup'], [], [])
        return project_data

    # Store the full path to a node inside the node as "fullPath", and another
    # raw copy as "fullPathRaw". Also recurse into that node if it's a group.
    # The raw copy may contain directory structure fragments (e.g. "some/path")
    #
    def build_full_paths(self, node, base_path, base_path_split):
        if node.get('path', False):
            base_path = base_path + [node['path']]
            base_path_split = base_path_split + node['path'].split('/')
        node['fullPathRaw'] = base_path
        node['fullPath'] = base_path_split
        if node['isa'] == 'PBXGroup':
            for child in node['children']:
                self.build_full_paths(child, base_path, base_path_split)

    # Get an object by its flat reference, or just return the "key" if it
    # isn't actually a key (24 char hexadecimal).
    def dereference(self, all_objects, key):
        if isinstance(key, basestring) and len(key) == 24 and re.search('^[0-9a-fA-F]+$', key):
            return all_objects[key]
        return key

    # Convert the Xcode flat key-value object layout to a deep layout
    def unflatten_object(self, all_objects, current_obj):
        if isinstance(current_obj, collections.Mapping):
            for key, value in current_obj.items():
                new_value = self.dereference(all_objects, value)
                if current_obj[key] != new_value or not isinstance(new_value, collections.Mapping):
                    current_obj[key] = new_value
                    self.unflatten_object(all_objects, new_value)
        elif isinstance(current_obj, collections.Iterable) and not isinstance(current_obj, basestring):
            for idx, value in enumerate(current_obj):
                new_value = self.dereference(all_objects, value)
                if current_obj[idx] != new_value:
                    current_obj[idx] = self.dereference(all_objects, value)
                    self.unflatten_object(all_objects, current_obj[idx])

    # Fix up any inconvenient keys
    def fix_keys(self, obj):
        key_remappings = {'lastKnownFileType': 'fileType', 'explicitFileType': 'fileType'}
        for key in list(set(key_remappings.keys()) & set(obj.keys())):
            obj[key_remappings[key]] = obj[key]
            del obj[key]

    # Get the files from a build phase
    def get_build_phase_files(self, build_phase_name, filter_func):
        build_phase = filter(lambda x: x['isa'] == build_phase_name, self.target['buildPhases'])[0]
        build_files = filter(filter_func, build_phase['files'])
        return [x['fileRef'] for x in build_files]

    # Get the truncated paths of all headers that start with the specified
    # relative path. Paths are read and returned as fully separated lists.
    # e.g. ['Some', 'Path', 'To', 'A', 'Header'] with relative_path of
    # ['Some', 'Path'] gets truncated to ['To', 'A', 'Header']
    #
    def movable_headers_relative_to(self, relative_path):
        rel_path_length = len(relative_path)
        result = filter(lambda path: len(path) >= rel_path_length and
                                     path[:rel_path_length] == relative_path, self.header_paths)
        return [path[rel_path_length:] for path in result]

    # Get the full path to where a linkable archive (library or framework)
    # is supposed to be.
    def get_linked_archive_path(self, architecture):
        return "%s/%s/%s" % (os.environ['OBJECT_FILE_DIR_%s' % os.environ['CURRENT_VARIANT']],
                             architecture,
                             os.environ['EXECUTABLE_NAME'])

    # Get the full path to our custom linked archive of the project
    def get_linked_ufw_archive_path(self, architecture):
        return self.get_linked_archive_path(architecture) + ".ufwbuild"

    # Get the full path to the executable of an archive
    def get_exe_path(self, node):
        path = os.environ['SOURCE_ROOT'] + "/" + "/".join(node['fullPath'])
        if node['fileType'] == 'wrapper.framework':
            # Frameworks are directories, so go one deeper
            path += "/" + os.path.splitext(node['fullPath'][-1])[0]
        return path

    # Command to link all objects of a single architecture
    def get_link_single_arch_command(self, architecture):
        cmd = ["%s/usr/bin/libtool" % os.environ['DT_TOOLCHAIN_DIR'],
               "-static",
               "-arch_only", architecture,
               "-syslibroot", os.environ['SDKROOT'],
               "-L%s" % os.environ['BUILT_PRODUCTS_DIR'],
               "-filelist", os.environ['LINK_FILE_LIST_%s_%s' % (os.environ['CURRENT_VARIANT'], architecture)]]
        if os.environ.get('OTHER_LDFLAGS', False):
            cmd += [os.environ['OTHER_LDFLAGS']]
        if os.environ.get('WARNING_LDFLAGS', False):
            cmd += [os.environ['WARNING_LDFLAGS']]
        cmd += ["-o", self.get_linked_ufw_archive_path(architecture)]
        return cmd

    # Command to link all archives into a universal archive
    def get_final_link_command(self):
        cmd = ["%s/usr/bin/libtool" % os.environ['DT_TOOLCHAIN_DIR'],
               "-static"]
        cmd += self.local_linked_archive_paths + self.build_state.slave_linked_archive_paths
        cmd += [self.get_exe_path(fw) for fw in self.static_frameworks]
        cmd += [self.get_exe_path(lib) for lib in self.static_libraries]
        cmd += ["-o", "%s/%s" % (os.environ['BUILT_PRODUCTS_DIR'], os.environ['EXECUTABLE_PATH'])]
        return cmd

    # Build up an environment for the slave process. This uses BUILD_ROOT
    # and TEMP_ROOT to convert all environment variables to values suitable
    # for the slave build environment so that xcodebuild doesn't try to build
    # in the project directory under "build".
    #
    def get_slave_environment(self):
        ignored = ['LD_MAP_FILE_PATH']
        build_root = os.environ['BUILD_ROOT']
        temp_root = os.environ['TEMP_ROOT']
        newenv = {}
        for key, value in os.environ.items():
            if key not in ignored and not key.startswith('LINK_FILE_LIST_'):
                if build_root in value or temp_root in value:
                    newenv[key] = value.replace(self.local_platform, self.other_platform)
        return newenv

    # Command to invoke xcodebuild on the slave platform.
    def get_slave_project_build_command(self):
        cmd = ["xcodebuild",
               "-project",
               os.environ['PROJECT_FILE_PATH'],
               "-target",
               os.environ['TARGET_NAME'],
               "-configuration",
               os.environ['CONFIGURATION'],
               "-sdk",
               self.other_platform + self.sdk_version]
        cmd += ["%s=%s" % (key, value) for key, value in self.get_slave_environment().items()]
        cmd += ["UFW_MASTER_PLATFORM=" + os.environ['PLATFORM_NAME']]
        cmd += [os.environ['ACTION']]
        return cmd


# Utility Functions

def is_master():
    return os.environ.get('UFW_MASTER_PLATFORM', os.environ['PLATFORM_NAME']) == os.environ['PLATFORM_NAME']

# Remove all subdirectories under a path.
def remove_subdirs(path, ignore_files):
    if os.path.exists(path):
        for filename in filter(lambda x: x not in ignore_files, os.listdir(path)):
            fullpath = path + "/" + filename
            if os.path.isdir(fullpath):
                log.info("Remove %s" % fullpath)
                shutil.rmtree(fullpath)

def ensure_parent_exists(path):
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)

def ensure_path_exists(path):
    if not os.path.isdir(path):
        os.makedirs(path)

def remove_path(path):
    if os.path.exists(path):
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

def move_file(src, dst):
    if src == dst or not os.path.isfile(src):
        return
    log.info("Move %s to %s" % (src, dst))
    ensure_parent_exists(dst)
    remove_path(dst)
    shutil.move(src, dst)

def copy_overwrite(src, dst):
    remove_path(dst)
    ensure_parent_exists(dst)
    shutil.copytree(src, dst, symlinks=True)

def attempt_symlink(link_path, link_to):
    # Only allow linking to an existing file
    os.stat(os.path.abspath(link_path + "/../" + link_to))

    # Only make the link if it hasn't already been made
    if not os.path.exists(link_path):
        log.info("Symlink %s -> %s" % (link_path, link_to))
        os.symlink(link_to, link_path)

# Takes the last entry in an array-based path and returns a normal path
# relative to base_path.
#
def top_level_file_path(base_path, path_list):
    return base_path + "/" + os.path.split(path_list[-1])[-1]

# Takes all entries in an array-based path and returns a normal path
# relative to base_path.
#
def full_file_path(base_path, path_list):
    return base_path + "/" + "/".join(path_list)

# Print a command before executing it.
# Also print out all output from the command to STDOUT.
#
def print_and_call(cmd):
    log.info("Cmd " + " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.info(p.communicate()[0])
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd)

# Special print-and-call command for the slave build that strips out
# xcodebuild's spammy list of environment variables.
#
def print_and_call_slave_build(cmd, other_platform):
    separator = '=== BUILD NATIVE TARGET '
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    result = p.communicate()[0].split(separator)
    if len(result) == 1:
        result = result[0]
    else:
        result = separator + result[1]
    log.info("Cmd " + " ".join(cmd) + "\n" + result)
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd)

def issue_warning(msg, *args, **kwargs):
    global issued_warnings
    issued_warnings = True
    log.warn(msg, *args, **kwargs)


# Main Application

# DerivedData should almost never appear in any framework, library, or header
# search paths. However, Xcode will sometimes add them in, so we check to make
# sure.
#
def check_for_derived_data_in_search_paths():
    search_path_keys = ["FRAMEWORK_SEARCH_PATHS", "LIBRARY_SEARCH_PATHS", "HEADER_SEARCH_PATHS"]
    for path_key in search_path_keys:
        path = os.environ[path_key]
        if "DerivedData" in path and "/../" in path:
            issue_warning("'%s' contains reference to 'DerivedData'." % path_key)

# Check to make sure nothing has been recompiled since we last linked.
def are_link_targets_clean(project):
    try:
        for arch in project.local_architectures:
            link_time = os.path.getmtime(project.get_linked_archive_path(arch))
            ufw_time = os.path.getmtime(project.get_linked_ufw_archive_path(arch))
            if not link_time or not ufw_time or link_time > ufw_time:
                return False
    except OSError:
        return False
    return True

def relink_project(project):
    for arch in project.local_architectures:
        print_and_call(project.get_link_single_arch_command(arch))

    if is_master():
        print_and_call(project.get_final_link_command())

# Xcode by default throws all public headers into the top level directory.
# This function moves them to their expected deep hierarchy.
#
def build_deep_header_hierarchy(project):
    header_path_top = config_deep_header_top
    if not header_path_top:
        header_path_top = os.path.commonprefix(project.header_paths)
    else:
        header_path_top = header_path_top.split('/')

    built_headers_path = os.environ['BUILT_PRODUCTS_DIR'] + "/" + os.environ['PUBLIC_HEADERS_FOLDER_PATH']
    movable_headers = project.movable_headers_relative_to(header_path_top)

    # Remove subdirs if they only contain files that have been rebuilt
    ignore_headers = filter(lambda x: not os.path.isfile(top_level_file_path(built_headers_path, x)), movable_headers)
    remove_subdirs(built_headers_path, [file[0] for file in ignore_headers])

    # Move rebuilt headers into their proper subdirs
    for header in movable_headers:
        move_file(top_level_file_path(built_headers_path, header), full_file_path(built_headers_path, header))

def add_symlinks_to_framework(project):
    base_dir = project.local_built_fw_path + "/"
    attempt_symlink(base_dir + "Versions/Current", os.environ['FRAMEWORK_VERSION'])
    if os.path.isdir(base_dir + "Versions/Current/Headers"):
        attempt_symlink(base_dir + "Headers", "Versions/Current/Headers")
    if os.path.isdir(base_dir + "Versions/Current/Resources"):
        attempt_symlink(base_dir + "Resources", "Versions/Current/Resources")
    attempt_symlink(base_dir + os.environ['EXECUTABLE_NAME'], "Versions/Current/" + os.environ['EXECUTABLE_NAME'])

def run_slave_build(project):
    print_and_call_slave_build(project.get_slave_project_build_command(), project.other_platform)

def build_embedded_framework(project):
    fw_path = project.local_built_fw_path
    embedded_path = project.local_built_embedded_fw_path
    fw_name = os.environ['WRAPPER_NAME']
    remove_path(embedded_path)
    ensure_path_exists(embedded_path)
    copy_overwrite(fw_path, embedded_path + "/" + fw_name)
    ensure_path_exists(embedded_path + "/Resources")
    symlink_source = "../" + fw_name + "/Resources/"
    symlink_path = embedded_path + "/Resources/"
    if os.path.isdir(fw_path + "/Resources"):
        for file in filter(lambda x: x != "Info.plist" and not x.endswith(".lproj"), os.listdir(fw_path + "/Resources")):
            attempt_symlink(symlink_path + file, symlink_source + file)

def run_build(build_state):

    project = Project("%s/%s" % (os.environ['PROJECT_FILE_PATH'], "project.pbxproj"))

    rebuild_needed = True

    if is_master():
        log.debug("Building as MASTER")

        if len(project.compilable_sources) == 0:
            raise Exception("No compilable sources found. Please add at least one source file to build target %s." % os.environ['TARGET_NAME'])

        if config_warn_derived_data:
            check_for_derived_data_in_search_paths()
        if config_warn_no_public_headers and len(project.public_headers) == 0:
            issue_warning('No headers in build target %s were marked public. Please move at least one header to "Public" in the "Copy Headers" build phase.' % os.environ['TARGET_NAME'])

        if os.path.exists(project.local_exe_path):
            rebuild_needed = os.path.getmtime(project.local_exe_path) > build_state.last_completion
    else:
        log.debug("Building as SLAVE")

    if rebuild_needed:
        if is_master():
            build_state.persist()
            run_slave_build(project)
            build_state.reload()
        else:
            build_state.set_slave_properties(project.local_architectures,
                                             project.local_linked_archive_paths,
                                             project.local_built_fw_path,
                                             project.local_built_embedded_fw_path)

        project.build_state = build_state

        if not are_link_targets_clean(project):
            relink_project(project)

        if config_deep_header_hierarchy:
            build_deep_header_hierarchy(project)

        add_symlinks_to_framework(project)
        build_embedded_framework(project)

        if is_master():
            # Copy to slave side.
            copy_overwrite(project.local_built_fw_path, build_state.slave_built_fw_path)
            copy_overwrite(project.local_built_embedded_fw_path, build_state.slave_built_embedded_fw_path)

            build_state.reset()
            build_state.last_completion = time.time()


if __name__ == "__main__":
    # TAG: BUILD SCRIPT (do not remove this comment)

    log_handler = logging.StreamHandler()
    log_handler.setFormatter(logging.Formatter("%(name)s (" + os.environ['PLATFORM_NAME'] + "): %(levelname)s: %(message)s"))
    log.addHandler(log_handler)
    log.setLevel(config_log_level)

    error_code = 0
    build_state = BuildState()
    prefix = "M" if is_master() else "S"
    log_handler.setFormatter(logging.Formatter("%(name)s (" + prefix + " " + os.environ['PLATFORM_NAME'] + "): %(levelname)s: %(message)s"))

    log.debug("Begin build process")

    if config_deep_header_top:
        config_deep_header_top = string.Template(config_deep_header_top).substitute(os.environ)

    try:
        run_build(build_state)
        if issued_warnings:
            if config_fail_on_warnings:
                error_code = 1
            log.warn("Build completed with warnings")
        else:
            log.info("Build completed")
    except Exception:
        traceback.print_exc(file=sys.stdout)
        build_state.reset()
        error_code = 1
        log.error("Build failed")
    finally:
        build_state.persist()
        sys.exit(error_code)
