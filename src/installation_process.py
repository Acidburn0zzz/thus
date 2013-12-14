#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  installation_process.py
#
#  This file was forked from Cnchi (graphical installer from Antergos)
#  Check it at https://github.com/antergos
#
#  Copyright 2013 Antergos (http://antergos.com/)
#  Copyright 2013 Manjaro (http://manjaro.org)
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

""" Installation thread module. Where the real installation happens """

import crypt
import info
import logging
import multiprocessing
import os
import queue
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as etree

import encfs
import auto_partition
import parted3.fs_module as fs
import canonical.misc as misc

from configobj import ConfigObj
import stat
import math

conf_file = '/etc/thus.conf'
configuration = ConfigObj(conf_file)
MHWS_SCRIPT = 'mhwd.sh'

## BEGIN: RSYNC-based file copy support
#CMD = 'unsquashfs -f -i -da 32 -fr 32 -d %(dest)s %(source)s'
CMD = 'rsync -ar --progress %(source)s %(dest)s'
PERCENTAGE_FORMAT = '%d/%d ( %.2f %% )'
from threading import Thread
import re
ON_POSIX = 'posix' in sys.builtin_module_names

# Update the value of the progress bar so that we get
# some movement
class FileCopyThread(Thread):
    def __init__(self, installer, current_file, total_files, source, dest, offset=0):
        self.our_current = current_file
        self.process = subprocess.Popen(
            (CMD % {
                'source': source,
                'dest': dest,
            }).split(),
            bufsize=1,
            stdout=subprocess.PIPE,
            close_fds=ON_POSIX
        )
        self.installer = installer
        self.total_files = total_files
        # in order for the progressbar to pick up where the last rsync ended,
        # we need to set the offset (because the total number of files is calculated before)
        self.offset = offset
        super(FileCopyThread, self).__init__()

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()

    def update_label(self, text):
        self.installer.queue_event('info', _("Copying '/%s'") % text)

    def update_progress(self, num_files):
        progress = (float(num_files)/float(self.total_files))
        self.installer.queue_event('percent', progress)
        #self.installer.queue_event('progress-info', PERCENTAGE_FORMAT % (num_files, self.total_files, (progress*100)))

    def run(self):
        num_files_copied = 0
        for line in iter(self.process.stdout.readline, b''):
            # small comment on this regexp.
            # rsync outputs three parameters in the progress.
            # xfer#x => i try to interpret it as 'file copy try no. x'
            # to-check=x/y, where:
            #  - x = number of files yet to be checked
            #  - y = currently calculated total number of files.
            # but if you're copying directory with some links in it, the xfer# might not be a
            # reliable counter. ( for one increase of xfer, many files may be created)
            # In case of manjaro, we pre-compute the total number of files.
            # therefore we can easily subtract x from y in order to get real files copied / processed count.
            m = re.findall(r'xfr#(\d+), ir-chk=(\d+)/(\d+)', line.decode())
            if m:
                # we've got a percentage update
                num_files_remaining = int(m[0][1])
                num_files_total_local = int(m[0][2])
                # adjusting the offset so that progressbar can be continuesly drawn
                num_files_copied = num_files_total_local - num_files_remaining + self.offset
                if num_files_copied % 100 == 0:
                    self.update_progress(num_files_copied)
            else:
                # we've got a filename!
                if num_files_copied % 100 == 0:
                    self.update_label(line.decode().strip())

        self.offset = num_files_copied

## END: RSYNC-based file copy support

class InstallError(Exception):
    """ Exception class called upon an installer error """
    def __init__(self, value):
        """ Initialize exception class """
        super().__init__(value)
        self.value = value
    def __str__(self):
        """ Returns exception message """
        return repr(self.value)

class InstallationProcess(multiprocessing.Process):
    """ Installation process thread class """
    def __init__(self, settings, callback_queue, mount_devices, \
                 fs_devices, ssd=None, alternate_package_list="", blvm=False):
        """ Initialize installation class """
        multiprocessing.Process.__init__(self)

        self.alternate_package_list = alternate_package_list

        self.callback_queue = callback_queue
        self.settings = settings

        # Save how we have been called
        # We need this in case we have to retry the installation
        parameters = {'mount_devices' : mount_devices,
         'fs_devices' : fs_devices,
         'ssd' : ssd,
         'alternate_package_list' : alternate_package_list,
         'blvm': blvm }
        self.settings.set('installer_thread_call', parameters)

        # This flag tells us if there is a lvm partition (from advanced install)
        # If it's true we'll have to add the 'lvm2' hook to mkinitcpio
        self.blvm = blvm

        self.method = self.settings.get('partition_mode')

        self.queue_event('info', _("Installing using the '%s' method") % self.method)

        self.ssd = ssd
        self.mount_devices = mount_devices

        # Set defaults
        self.desktop_manager = 'none'
        self.network_manager = 'NetworkManager'
        self.card = []
        # Packages to be removed
        self.conflicts = []

        self.fs_devices = fs_devices

        self.running = True
        self.error = False

        self.special_dirs_mounted = False

        # Initialize some vars that are correctly initialized elsewhere (pylint complains about it)
        self.auto_device = ""
        self.arch = ""
        self.initramfs = ""
        self.kernel = ""
        self.vmlinuz = ""
        self.dest_dir = ""

    def queue_fatal_event(self, txt):
        """ Queues the fatal event and exits process """
        self.error = True
        self.running = False
        self.queue_event('error', txt)
        self.callback_queue.join()
        # Is this really necessary?
        os._exit(0)

    def queue_event(self, event_type, event_text=""):
        try:
            self.callback_queue.put_nowait((event_type, event_text))
        except queue.Full:
            pass

    @misc.raise_privileges
    def run(self):
        """ Run installation """
        # Common vars
        self.packages = []

        self.dest_dir = "/install"
        if not os.path.exists(self.dest_dir):
            os.makedirs(self.dest_dir)
        else:
            # If we're recovering from a failed/stoped install, there'll be
            # some mounted directories. Try to unmount them first.
            # We use unmount_all from auto_partition to do this.
            auto_partition.unmount_all(self.dest_dir)

        # get settings
        self.distribution_name = configuration['distribution']['DISTRIBUTION_NAME']
        self.distribution_version = configuration['distribution']['DISTRIBUTION_VERSION']
        self.live_user = configuration['install']['LIVE_USER_NAME']
        self.media = configuration['install']['LIVE_MEDIA_SOURCE']
        self.media_desktop = configuration['install']['LIVE_MEDIA_DESKTOP']
        self.media_type = configuration['install']['LIVE_MEDIA_TYPE']
        self.kernel = configuration['install']['KERNEL']

        self.vmlinuz = "vmlinuz-%s" % self.kernel
        self.initramfs = "initramfs-%s" % self.kernel

        self.arch = os.uname()[-1]

        # Create and format partitions

        if self.method == 'automatic':
            self.auto_device = self.settings.get('auto_device')

            self.queue_event('debug', _("Creating partitions and their filesystems in %s") % self.auto_device)

            # If no key password is given a key file is generated and stored in /boot
            # (see auto_partition.py)

            try:
                auto = auto_partition.AutoPartition(self.dest_dir,
                                                    self.auto_device,
                                                    self.settings.get("use_luks"),
                                                    self.settings.get("use_lvm"),
                                                    self.settings.get("luks_key_pass"),
                                                    self.settings.get("use_home"),
                                                    self.callback_queue)
                auto.run()

                # Get mount_devices and fs_devices
                # (mount_devices will be used when configuring GRUB in modify_grub_default)
                # (fs_devices  will be used when configuring the fstab file)
                self.mount_devices = auto.get_mount_devices()
                self.fs_devices = auto.get_fs_devices()
            except subprocess.CalledProcessError as err:
                logging.error(err.output)
                self.queue_event('error', _("Error creating partitions and their filesystems"))
                return

        if self.method == 'alongside':
            # Alongside method shrinks selected partition
            # and creates root and swap partition in the available space
            boot_partition, root_partition = fs.shrink(self.mount_devices["alongside"])
            # Alongside method formats root by default (as it is always a new partition)
            (error, msg) = fs.create_fs(self.mount_devices["/"], "ext4")

        if self.method == 'advanced':
            root_partition = self.mount_devices["/"]

            # NOTE: Advanced method formats root by default in installation_advanced

            #if root_partition in self.fs_devices:
            #    root_fs = self.fs_devices[root_partition]
            #else:
            #    root_fs = "ext4"

            if "/boot" in self.mount_devices:
                boot_partition = self.mount_devices["/boot"]
            else:
                boot_partition = ""

            if "swap" in self.mount_devices:
                swap_partition = self.mount_devices["swap"]
            else:
                swap_partition = ""

        # Create the directory where we will mount our new root partition
        if not os.path.exists(self.dest_dir):
            os.mkdir(self.dest_dir)

        # Mount root and boot partitions (only if it's needed)
        # Not doing this in automatic mode as AutoPartition class mounts the root and boot devices itself.
        if self.method == 'alongside' or self.method == 'advanced':
            try:
                txt = _("Mounting partition %s into %s directory") % (root_partition, self.dest_dir)
                self.queue_event('debug', txt)
                subprocess.check_call(['mount', root_partition, self.dest_dir])
                # We also mount the boot partition if it's needed
                subprocess.check_call(['mkdir', '-p', '%s/boot' % self.dest_dir])
                if "/boot" in self.mount_devices:
                    txt = _("Mounting partition %s into %s/boot directory") % (boot_partition, self.dest_dir)
                    self.queue_event('debug', txt)
                    subprocess.check_call(['mount', boot_partition, "%s/boot" % self.dest_dir])
            except subprocess.CalledProcessError as err:
                logging.error(err)
                self.queue_fatal_event(_("Couldn't mount root and boot partitions"))
                return False

        # In advanced mode, mount all partitions (root and boot are already mounted)
        if self.method == 'advanced':
            for path in self.mount_devices:
                mount_part = self.mount_devices[path]
                if mount_part != root_partition and mount_part != boot_partition and mount_part != swap_partition:
                    try:
                        mount_dir = self.dest_dir + path
                        if not os.path.exists(mount_dir):
                            os.makedirs(mount_dir)
                        txt = _("Mounting partition %s into %s directory") % (mount_part, mount_dir)
                        self.queue_event('debug', txt)
                        subprocess.check_call(['mount', mount_part, mount_dir])
                    except subprocess.CalledProcessError as err:
                        # We will continue as root and boot are already mounted
                        logging.warning(err)
                        self.queue_event('debug', _("Can't mount %s in %s") % (mount_part, mount_dir))


        # Nasty workaround:
        # If pacman was stoped and /var is in another partition than root
        # (so as to be able to resume install), database lock file will still be in place.
        # We must delete it or this new installation will fail

        db_lock = os.path.join(self.dest_dir, "var/lib/pacman/db.lck")
        if os.path.exists(db_lock):
            with misc.raised_privileges():
                os.remove(db_lock)
            logging.debug(_("%s deleted"), db_lock)


        # Create some needed folders
        try:
            subprocess.check_call(['mkdir', '-p', '%s/var/lib/pacman' % self.dest_dir])
            subprocess.check_call(['mkdir', '-p', '%s/etc/pacman.d/gnupg/' % self.dest_dir])
            subprocess.check_call(['mkdir', '-p', '%s/var/log/' % self.dest_dir])
        except subprocess.CalledProcessError as err:
            logging.error(err)
            self.queue_fatal_event(_("Can't create necessary directories on destination system"))
            return False

        all_ok = True

        try:
            self.queue_event('debug', _('Install System ...'))
            # very slow ...
            self.install_system()

            subprocess.check_call(['mkdir', '-p', '%s/var/log/' % self.dest_dir])
            self.queue_event('debug', _('System installed.'))

            self.queue_event('debug', _('Configuring system ...'))
            self.configure_system()
            self.queue_event('debug', _('System configured.'))

            # Install boot loader (always after running mkinitcpio)
            if self.settings.get('install_bootloader'):
                self.queue_event('debug', _('Installing bootloader ...'))
                self.install_bootloader()

        except subprocess.CalledProcessError as err:
            logging.error(err)
            self.queue_fatal_event("CalledProcessError.output = %s" % e.output)
            all_ok = False
        except InstallError as err:
            logging.error(err)
            self.queue_fatal_event(err.value)
            all_ok = False
        except:
            # unknown error
            logging.error(_("Unknown error"))
            self.queue_fatal_event(_("Unknown error"))
            self.running = False
            self.error = True
            all_ok = False

        if all_ok is False:
            return False
        else:
            # Last but not least, copy Thus log to new installation
            datetime = time.strftime("%Y%m%d") + "-" + time.strftime("%H%M%S")
            dst = os.path.join(self.dest_dir, "var/log/thus-%s.log" % datetime)
            try:
                shutil.copy("/tmp/thus.log", dst)
            except FileNotFoundError:
                logging.warning(_("Can't copy Thus log to %s") % dst)
            except FileExistsError:
                pass
            # Unmount everything
            self.chroot_umount_special_dirs()
            source_dirs = { "source", "source_desktop" }
            for p in source_dirs:
                p = os.path.join("/", p)
                (fsname, fstype, writable) = misc.mount_info(p)
                if fsname:
                    try:
                        txt = _("Unmounting %s") % p
                        self.queue_event('debug', txt)
                        subprocess.check_call(['umount', '-l', p])
                    except subprocess.CalledProcessError as err:
                        logging.warning(err)
                        self.queue_event('debug', _("Can't unmount %s") % p)
            self.queue_event('debug', "Mounted devices: %s" % self.mount_devices)
            for path in self.mount_devices:
                mount_part = self.mount_devices[path]
                mount_dir = self.dest_dir + path
                if path != '/' and path != 'swap' and path !='':
                    try:

                        txt = _("Unmounting %s") % mount_dir
                        self.queue_event('debug', txt)
                        subprocess.check_call(['umount', '-l', mount_dir])
                    except subprocess.CalledProcessError as err:
                        # We will continue as root and boot are already mounted
                        logging.warning(err)
                        self.queue_event('debug', _("Can't unmount %s") % mount_dir)
            # now we can unmount /install
            (fsname, fstype, writable) = misc.mount_info(self.dest_dir)
            if fsname:
                try:
                    txt = _("Unmounting %s") % self.dest_dir
                    self.queue_event('debug', txt)
                    subprocess.check_call(['umount', '-l', self.dest_dir])
                except subprocess.CalledProcessError as err:
                    logging.warning(err)
                    self.queue_event('debug', _("Can't unmount %s") % p)
            # Installation finished successfully
            self.queue_event('info', _("Installation finished successfully."))
            self.queue_event("finished")
            self.running = False
            self.error = False
            return True

    def install_system(self):
        """ Copies all files to target """
        # mount the media location.
        try:
            if(not os.path.exists(self.dest_dir)):
                os.mkdir(self.dest_dir)
            if(not os.path.exists("/source")):
                os.mkdir("/source")
            if(not os.path.exists("/source_desktop")):
                os.mkdir("/source_desktop")
            # find the squashfs..
            if(not os.path.exists(self.media)):
                self.queue_event('debug', _("Base filesystem does not exist! Critical error (exiting)."))
                sys.exit(1) # change to report
            if(not os.path.exists(self.media_desktop)):
                self.queue_event('debug', _("Desktop filesystem does not exist! Critical error (exiting)."))
                sys.exit(1) # change to report

            # Mount the installation media
            subprocess.check_call(["mount", self.media, "/source/", "-t", self.media_type, "-o", "loop"])
            subprocess.check_call(["mount", self.media_desktop, "/source_desktop/", "-t", self.media_type, "-o", "loop"])

            # walk root filesystem
            SOURCE = "/source/"
            DEST = self.dest_dir
            directory_times = []
            # index the files
            self.queue_event('info', "Indexing files to be copied...")
            p1 = subprocess.Popen(["unsquashfs", "-l", self.media], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(["wc","-l"], stdin=p1.stdout, stdout=subprocess.PIPE)
            output1 = p2.communicate()[0]
            self.queue_event('info', _("Indexing files to be copied ..."))
            p1 = subprocess.Popen(["unsquashfs", "-l", self.media_desktop], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(["wc","-l"], stdin=p1.stdout, stdout=subprocess.PIPE)
            output2 = p2.communicate()[0]
            our_total = int(float(output1)+float(output2))
            self.queue_event('info', _("Extracting root-image ..."))
            our_current = 0
            #t = FileCopyThread(self, our_total, self.media, DEST)
            t = FileCopyThread(self, our_current, our_total, SOURCE, DEST)
            t.start()
            t.join()
            # walk desktop filesystem
            SOURCE = "/source_desktop/"
            DEST = self.dest_dir
            directory_times = []
            self.queue_event('info', _("Extracting desktop-image ..."))
            our_current = int(output1)
            #t = FileCopyThread(self, our_total, self.media_desktop, DEST)
            t = FileCopyThread(self, our_current, our_total, SOURCE, DEST, t.offset)
            t.start()
            t.join()
            # this is purely out of aesthetic reasons. Because we're reading of the queue
            # once 3 seconds, good chances are we're going to miss the 100% file copy.
            # therefore it would be nice to show 100% to the user so he doesn't panick that
            # not all of the files copied.
            self.queue_event('percent', 1.00)
            self.queue_event('progress-info', PERCENTAGE_FORMAT % (our_total, our_total, 100))
            for dirtime in directory_times:
                (directory, atime, mtime) = dirtime
                try:
                    self.queue_event('info', _("Restoring meta-information on %s") % directory)
                    os.utime(directory, (atime, mtime))
                except OSError:
                    pass

        except Exception as err:
            logging.error(err)
            import traceback
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_tb(exc_traceback, limit=1, file=sys.stdout)

    def chroot_mount_special_dirs(self):
        """ Mount special directories for our chroot """
        # Don't try to remount them
        if self.special_dirs_mounted:
            self.queue_event('debug', _("Special dirs already mounted."))
            return

        special_dirs = [ "sys", "proc", "dev/pts", "dev" ]
        for s_dir in special_dirs:
            mydir = os.path.join(self.dest_dir, s_dir)
            if not os.path.exists(mydir):
                os.makedirs(mydir)

        mydir = os.path.join(self.dest_dir, "sys")

        subprocess.check_call(["mount", "-t", "sysfs", "/sys", mydir])
        subprocess.check_call(["chmod", "555", mydir])

        mydir = os.path.join(self.dest_dir, "proc")
        subprocess.check_call(["mount", "-t", "proc", "/proc", mydir])
        subprocess.check_call(["chmod", "555", mydir])

        mydir = os.path.join(self.dest_dir, "dev")
        subprocess.check_call(["mount", "-o", "bind", "/dev", mydir])

        mydir = os.path.join(self.dest_dir, "dev/pts")
        subprocess.check_call(["mount", "-t", "devpts", "/dev/pts", mydir])
        subprocess.check_call(["chmod", "555", mydir])

        self.special_dirs_mounted = True

    def chroot_umount_special_dirs(self):
        """ Umount special directories for our chroot """
        # Do not umount if they're not mounted
        if not self.special_dirs_mounted:
            self.queue_event('debug', _("Special dirs are not mounted. Skipping."))
            return

        special_dirs = [ "proc", "sys", "dev/pts", "dev" ]

        for s_dir in special_dirs:
            mydir = os.path.join(self.dest_dir, s_dir)
            try:
                subprocess.check_call(["umount", "-l", mydir])
            except:
                self.queue_event('warning', _("Unable to umount %s") % mydir)

        self.special_dirs_mounted = False

    def chroot(self, cmd, stdin=None, stdout=None):
        """ Runs command inside the chroot """
        run = [ 'chroot', self.dest_dir ]

        for element in cmd:
            run.append(element)

        try:
            proc = subprocess.Popen(run,
                                    stdin=stdin,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            out = proc.communicate()[0]
            logging.debug(out.decode())
        except OSError as err:
            logging.exception(_("Error running command: %s"), err.strerror)
            raise

    def is_running(self):
        """ Checks if thread is running """
        return self.running

    def is_ok(self):
        """ Checks if an error has been issued """
        return not self.error

    def copy_network_config(self):
        """ Copies Network Manager configuration """
        source_nm = "/etc/NetworkManager/system-connections/"
        target_nm = "%s/etc/NetworkManager/system-connections/" % self.dest_dir

        # Sanity checks.  We don't want to do anything if a network
        # configuration already exists on the target
        if os.path.exists(source_nm) and os.path.exists(target_nm):
            for network in os.listdir(source_nm):
                # Skip LTSP live
                if network == "LTSP":
                    continue

                source_network = os.path.join(source_nm, network)
                target_network = os.path.join(target_nm, network)

                if os.path.exists(target_network):
                    continue

                shutil.copy(source_network, target_network)

    def auto_fstab(self):
        """ Create /etc/fstab file """

        all_lines = []
        all_lines.append("# /etc/fstab: static file system information.")
        all_lines.append("#")
        all_lines.append("# Use 'blkid' to print the universally unique identifier for a")
        all_lines.append("# device; this may be used with UUID= as a more robust way to name devices")
        all_lines.append("# that works even if disks are added and removed. See fstab(5).")
        all_lines.append("#")
        all_lines.append("# <file system> <mount point>   <type>  <options>       <dump>  <pass>")
        all_lines.append("#")

        root_ssd = 0

        for path in self.mount_devices:
            opts = 'defaults'
            chk = '0'
            parti = self.mount_devices[path]
            part_info = fs.get_info(parti)
            uuid = part_info['UUID']
            if parti in self.fs_devices:
                myfmt = self.fs_devices[parti]
            else:
                # It hasn't any filesystem defined
                continue

            # Take care of swap partitions
            if "swap" in myfmt:
                all_lines.append("UUID=%s %s %s %s 0 %s" % (uuid, path, myfmt, opts, chk))
                logging.debug(_("Added to fstab : UUID=%s %s %s %s 0 %s"), uuid, path, myfmt, opts, chk)
                continue

            # fstab uses vfat to mount fat16 and fat32 partitions
            if "fat" in myfmt:
                myfmt = 'vfat'

            # Avoid adding a partition to fstab when
            # it has no mount point (swap has been checked before)
            if path == "":
                continue

            if path == '/':
                chk = '1'
                opts = "rw,relatime,data=ordered"
            else:
                full_path = os.path.join(self.dest_dir, path)
                subprocess.check_call(["mkdir", "-p", full_path])

            if self.ssd != None:
                for i in self.ssd:
                    if i in self.mount_devices[path] and self.ssd[i]:
                        opts = 'defaults,noatime,nodiratime'
                        # As of linux kernel version 3.7, the following
                        # filesystems support TRIM: ext4, btrfs, JFS, and XFS.
                        # If using a TRIM supported SSD, discard is a valid mount option for swap
                        if myfmt == 'ext4' or myfmt == 'btrfs' or myfmt == 'jfs' or myfmt == 'xfs' or myfmt == 'swap':
                            opts += ',discard'
                        if path == '/':
                            root_ssd = 1

            all_lines.append("UUID=%s %s %s %s 0 %s" % (uuid, path, myfmt, opts, chk))

        if root_ssd:
            all_lines.append("tmpfs /tmp tmpfs defaults,noatime,mode=1777 0 0")

        full_text = '\n'.join(all_lines)
        full_text += '\n'

        with open('%s/etc/fstab' % self.dest_dir, 'w') as fstab_file:
            fstab_file.write(full_text)

    def install_bootloader(self):
        """ Installs bootloader """
        bootloader = self.settings.get('bootloader_type')

        if bootloader == "GRUB2":
            self.install_bootloader_grub2_bios()
        elif bootloader == "UEFI_x86_64" or bootloader == "UEFI_i386":
            self.install_bootloader_grub2_efi(bootloader)

    def modify_grub_default(self):
        """ If using LUKS, we need to modify GRUB_CMDLINE_LINUX to load our root encrypted partition
            This scheme can be used in the automatic installation option only (at this time) """

        default_dir = os.path.join(self.dest_dir, "etc/default")
        default_grub = os.path.join(default_dir, "grub")
        if not os.path.exists(default_dir):
            os.mkdir(default_dir)
 
        if self.method == 'automatic' and self.settings.get('use_luks'):
            root_device = self.mount_devices["/"]
            boot_device = self.mount_devices["/boot"]

            # Let GRUB automatically add the kernel parameters for root encryption
            if self.settings.get("luks_key_pass") == "":
                default_line = 'GRUB_CMDLINE_LINUX="cryptdevice=%s:cryptManjaro cryptkey=%s:ext2:/.keyfile-root"' % (root_device, boot_device)
            else:
                default_line = 'GRUB_CMDLINE_LINUX="cryptdevice=%s:cryptManjaro"' % root_device

            # Disable the usage of UUIDs for the rootfs:
            disable_uuid_line = 'GRUB_DISABLE_LINUX_UUID=true'

            with open(default_grub, 'r') as grub_file:
                lines = [x.strip() for x in grub_file.readlines()]

            for i in range(len(lines)):
                if lines[i].startswith("#GRUB_CMDLINE_LINUX") or lines[i].startswith("GRUB_CMDLINE_LINUX"):
                    lines[i] = default_line
                elif lines[i].startswith("#GRUB_DISABLE_LINUX_UUID") or lines[i].startswith("GRUB_DISABLE_LINUX_UUID"):
                    lines[i] = disable_uuid_line
                elif lines[i].startswith("#GRUB_DISTRIBUTOR") or lines[i].startswith("GRUB_DISTRIBUTOR"):
                    lines[i] = "GRUB_DISTRIBUTOR=Manjaro"

            with open(default_grub, 'w') as grub_file:
                grub_file.write("\n".join(lines) + "\n")

        # Add GRUB_DISABLE_SUBMENU=y to avoid bug https://bugs.archlinux.org/task/37904
        #with open(default_grub, 'a') as grub_file:
        #    grub_file.write("\n# See bug https://bugs.archlinux.org/task/37904\n")
        #    grub_file.write("GRUB_DISABLE_SUBMENU=y\n\n")

    def install_bootloader_grub2_bios(self):
        """ Install bootloader in a BIOS system """
        grub_device = self.settings.get('bootloader_device')
        self.queue_event('info', _("Installing GRUB(2) BIOS boot loader in %s") % grub_device)

        self.modify_grub_default()

        grub_d_dir = os.path.join(self.dest_dir, "etc/grub.d")

        if not os.path.exists(grub_d_dir):
            os.makedirs(grub_d_dir)

        try:
            shutil.copy2("/etc/grub.d/10_linux", grub_d_dir)
        except FileNotFoundError:
            self.queue_event('warning', _("ERROR installing GRUB(2) BIOS."))
            return
        except FileExistsError:
            pass

        self.install_bootloader_grub2_locales()

        locale = self.settings.get("locale")
        self.chroot_mount_special_dirs()
        self.chroot(['sh', '-c', 'LANG=%s grub-mkconfig -o /boot/grub/grub.cfg' % locale])
        self.chroot(['grub-install', '--directory=/usr/lib/grub/i386-pc',
                  '--target=i386-pc', '--boot-directory=/boot',  '--recheck',
                  grub_device])

        self.chroot_umount_special_dirs()

        core_path = os.path.join(self.dest_dir, "boot/grub/i386-pc/core.img")
        if os.path.exists(core_path):
            self.queue_event('info', _("GRUB(2) BIOS has been successfully installed."))
        else:
            self.queue_event('warning', _("ERROR installing GRUB(2) BIOS."))

    def install_bootloader_grub2_efi(self, arch):
        """ Install bootloader in a UEFI system """
        uefi_arch = "x86_64"
        spec_uefi_arch = "x64"

        if arch == "UEFI_i386":
            uefi_arch = "i386"
            spec_uefi_arch = "ia32"

        grub_device = self.settings.get('bootloader_device')
        self.queue_event('info', _("Installing GRUB(2) UEFI %s boot loader in %s") % (uefi_arch, grub_device))

        self.modify_grub_default()

        self.chroot_mount_special_dirs()

        # grub2-efi installation isn't done in a chroot because when efibootmgr
        # runs it doesn't detect a uefi environment and fails to add a new uefi
        # boot entry.
        subprocess.check_call(['grub-install --target=%s-efi --efi-directory=/install/boot/efi --bootloader-id=manjaro_grub '
            '--boot-directory=/install/boot --recheck' % uefi_arch], shell=True)

        self.chroot_umount_special_dirs()

        self.install_bootloader_grub2_locales()

        locale = self.settings.get("locale")
        self.chroot_mount_special_dirs()
        self.chroot(['sh', '-c', 'LANG=%s grub-mkconfig -o /boot/grub/grub.cfg' % locale])
        self.chroot_umount_special_dirs()

        # TODO: Create a boot entry for Manjaro in the UEFI boot manager (is this necessary?)

        #grub_cfg = "%s/boot/grub/grub.cfg" % self.dest_dir
        #grub_standalone = "%s/boot/efi/EFI/manjaro_grub/grub%s_standalone.cfg" % (self.dest_dir, spec_uefi_arch)
        #try:
        #    shutil.copy2(grub_cfg, grub_standalone)
        #except FileNotFoundError:
        #    self.queue_event('warning', _("ERROR installing GRUB(2) configuration file."))
        #    return
        #except FileExistsError:
        #    # ignore if already exists
        #    pass
        #
        #self.chroot_mount_special_dirs()
        #self.chroot(['grub-mkstandalone', \
        #          '--directory=/usr/lib/grub/%s-efi' % uefi_arch, \
        #          '--format=%s-efi' % uefi_arch, \
        #          '--compression="xz"', \
        #          '--output="/boot/efi/EFI/manjaro_grub/grub%s_standalone.efi' % spec_uefi_arch, \
        #          'boot/grub/grub.cfg'])
        #self.chroot_umount_special_dirs()

    def install_bootloader_grub2_locales(self):
        """ Install Grub2 locales """
        dest_locale_dir = os.path.join(self.dest_dir, "boot/grub/locale")

        if not os.path.exists(dest_locale_dir):
            os.makedirs(dest_locale_dir)

        grub_mo = os.path.join(self.dest_dir, "usr/share/locale/en@quot/LC_MESSAGES/grub.mo")

        try:
            shutil.copy2(grub_mo, os.path.join(dest_locale_dir, "en.mo"))
        except FileNotFoundError:
            self.queue_event('warning', _("ERROR installing GRUB(2) locale."))
        except FileExistsError:
            # Ignore if already exists
            pass

    def enable_services(self, services):
        """ Enables all services that are in the list 'services' """
        for name in services:
            self.chroot(['systemctl', 'enable', name + ".service"])
            self.queue_event('debug', _('Enabled %s service.') % name)

    def change_user_password(self, user, new_password):
        """ Changes the user's password """
        try:
            shadow_password = crypt.crypt(new_password,"$6$%s$" % user)
        except:
            self.queue_event('warning', _('Error creating password hash for user %s') % user)
            return False

        try:
            self.chroot(['usermod', '-p', shadow_password, user])
        except:
            self.queue_event('warning', _('Error changing password for user %s') % user)
            return False

        return True

    def auto_timesetting(self):
        """ Set hardware clock """
        subprocess.check_call(["hwclock", "--systohc", "--utc"])
        shutil.copy2("/etc/adjtime", "%s/etc/" % self.dest_dir)

    def set_mkinitcpio_hooks_and_modules(self, hooks, modules):
        """ Set up mkinitcpio.conf """
        self.queue_event('debug', _('Setting hooks and modules in mkinitcpio.conf'))
        self.queue_event('debug', 'HOOKS="%s"' % ' '.join(hooks))
        self.queue_event('debug', 'MODULES="%s"' % ' '.join(modules))

        with open("/etc/mkinitcpio.conf") as mkinitcpio_file:
            mklins = [x.strip() for x in mkinitcpio_file.readlines()]

        for i in range(len(mklins)):
            if mklins[i].startswith("HOOKS"):
                mklins[i] = 'HOOKS="%s"' % ' '.join(hooks)
            elif mklins[i].startswith("MODULES"):
                mklins[i] = 'MODULES="%s"' % ' '.join(modules)

        path = os.path.join(self.dest_dir, "etc/mkinitcpio.conf")
        with open(path, "w") as mkinitcpio_file:
            mkinitcpio_file.write("\n".join(mklins) + "\n")

    def run_mkinitcpio(self):
        """ Runs mkinitcpio """
        # Add lvm and encrypt hooks if necessary

        hooks = [ "base", "udev", "autodetect", "modconf", "block" ]
        modules = []

        # It is important that the encrypt hook comes before the filesystems hook
        # (in case you are using LVM on LUKS, the order should be: encrypt lvm2 filesystems)

        if self.settings.get("use_luks"):
            hooks.append("encrypt")
            modules.extend([ "dm_mod", "dm_crypt", "ext4", "aes-x86_64", "sha256", "sha512" ])

        if self.blvm or self.settings.get("use_lvm"):
            hooks.append("lvm2")

        hooks.extend([ "filesystems", "keyboard", "fsck" ])

        self.set_mkinitcpio_hooks_and_modules(hooks, modules)

        # run mkinitcpio on the target system
        self.chroot_mount_special_dirs()
        self.chroot(["/usr/bin/mkinitcpio", "-p", self.kernel])
        self.chroot_umount_special_dirs()

    def uncomment_locale_gen(self, locale):
        """ Uncomment selected locale in /etc/locale.gen """
        #self.chroot(['sed', '-i', '-r', '"s/#(.*%s)/\1/g"' % locale, "/etc/locale.gen"])

        text = []
        with open("%s/etc/locale.gen" % self.dest_dir, "r") as gen:
            text = gen.readlines()

        with open("%s/etc/locale.gen" % self.dest_dir, "w") as gen:
            for line in text:
                if locale in line and line[0] == "#":
                    # uncomment line
                    line = line[1:]
                gen.write(line)

    def check_output(self, command):
        """ Helper function to run a command """
        return subprocess.check_output(command.split()).decode().strip("\n")

    def set_autologin(self):
        """ Enables automatic login for the installed desktop manager """
        username = self.settings.get('username')
        self.queue_event('info', _("%s: Enable automatic login for user %s.") % (self.desktop_manager, username))
                
        if self.desktop_manager == 'mdm':
            # Systems with MDM as Desktop Manager
            mdm_conf_path = os.path.join(self.dest_dir, "etc/mdm/custom.conf")
            if os.path.exists(mdm_conf_path):
                with open(mdm_conf_path, "r") as mdm_conf:
                    text = mdm_conf.readlines()
                with open(mdm_conf_path, "w") as mdm_conf:
                    for line in text:
                         if '[daemon]' in line:
                             line = '[daemon]\nAutomaticLogin=%s\nAutomaticLoginEnable=True\n' % username
                         mdm_conf.write(line)
            else:
                with open(mdm_conf_path, "w") as mdm_conf:
                    mdm_conf.write('# Thus - Enable automatic login for user\n')
                    mdm_conf.write('[daemon]\n')
                    mdm_conf.write('AutomaticLogin=%s\n' % username)
                    mdm_conf.write('AutomaticLoginEnable=True\n')
        elif self.desktop_manager == 'gdm':
            # Systems with GDM as Desktop Manager
            gdm_conf_path = os.path.join(self.dest_dir, "etc/gdm/custom.conf")
            if os.path.exists(gdm_conf_path):
                with open(gdm_conf_path, "r") as gdm_conf:
                    text = gdm_conf.readlines()
                with open(gdm_conf_path, "w") as gdm_conf:
                    for line in text:
                         if '[daemon]' in line:
                             line = '[daemon]\nAutomaticLogin=%s\nAutomaticLoginEnable=True\n' % username
                         gdm_conf.write(line)
            else:
                with open(gdm_conf_path, "w") as gdm_conf:
                    gdm_conf.write('# Thus - Enable automatic login for user\n')
                    gdm_conf.write('[daemon]\n')
                    gdm_conf.write('AutomaticLogin=%s\n' % username)
                    gdm_conf.write('AutomaticLoginEnable=True\n')
        elif self.desktop_manager == 'kdm':
            # Systems with KDM as Desktop Manager
            kdm_conf_path = os.path.join(self.dest_dir, "usr/share/config/kdm/kdmrc")
            text = []
            with open(kdm_conf_path, "r") as kdm_conf:
                text = kdm_conf.readlines()
            with open(kdm_conf_path, "w") as kdm_conf:
                for line in text:
                    if '#AutoLoginEnable=true' in line:
                        line = 'AutoLoginEnable=true\n'
                    if 'AutoLoginUser=' in line:
                        line = 'AutoLoginUser=%s\n' % username
                    kdm_conf.write(line)
        elif self.desktop_manager == 'lxdm':
            # Systems with LXDM as Desktop Manager
            lxdm_conf_path = os.path.join(self.dest_dir, "etc/lxdm/lxdm.conf")
            text = []
            with open(lxdm_conf_path, "r") as lxdm_conf:
                text = lxdm_conf.readlines()
            with open(lxdm_conf_path, "w") as lxdm_conf:
                for line in text:
                    if '# autologin=dgod' in line:
                        line = 'autologin=%s\n' % username
                    lxdm_conf.write(line)
        elif self.desktop_manager == 'lightdm':
            # Systems with LightDM as Desktop Manager
            # Ideally, we should use configparser for the ini conf file,
            # but we just do a simple text replacement for now, as it worksforme(tm)
            lightdm_conf_path = os.path.join(self.dest_dir, "etc/lightdm/lightdm.conf")
            text = []
            with open(lightdm_conf_path, "r") as lightdm_conf:
                text = lightdm_conf.readlines()
            with open(lightdm_conf_path, "w") as lightdm_conf:
                for line in text:
                    if '#autologin-user=' in line:
                        line = 'autologin-user=%s\n' % username
                    lightdm_conf.write(line)
        elif self.desktop_manager == 'slim':
            # Systems with Slim as Desktop Manager
            slim_conf_path = os.path.join(self.dest_dir, "etc/slim.conf")
            text = []
            with open(slim_conf_path, "r") as slim_conf:
                text = slim_conf.readlines()
            with open(slim_conf_path, "w") as slim_conf:
                for line in text:
                    if 'auto_login' in line:
                        line = 'auto_login yes\n'
                    if 'default_user' in line:
                        line = 'default_user %s\n' % username
                    slim_conf.write(line)

    def configure_system(self):
        """ Final install steps
            Set clock, language, timezone
            Run mkinitcpio
            Populate pacman keyring
            Setup systemd services
            ... and more """

        self.queue_event('action', _("Configuring your new system"))

        self.auto_fstab()
        self.queue_event('debug', _('fstab file generated.'))

        # Copy configured networks in Live medium to target system
        if self.network_manager == 'NetworkManager':
            self.copy_network_config()

        self.queue_event('debug', _('Network configuration copied.'))

        self.queue_event("action", _("Configuring your new system"))
        self.queue_event('pulse')

        # enable services
        self.enable_services([ self.network_manager, 'remote-fs.target' ])

        cups_service = os.path.join(self.dest_dir, "usr/lib/systemd/system/cups.service")
        if os.path.exists(cups_service):
            self.enable_services([ 'cups' ])

        self.queue_event('debug', 'Enabled installed services.')

        # Wait FOREVER until the user sets the timezone
        while self.settings.get('timezone_done') is False:
            # wait five seconds and try again
            time.sleep(5)

        if self.settings.get("use_ntp"):
            self.enable_services(["ntpd"])

        # Set timezone
        zoneinfo_path = os.path.join("/usr/share/zoneinfo", self.settings.get("timezone_zone"))
        self.chroot(['ln', '-s', zoneinfo_path, "/etc/localtime"])

        self.queue_event('debug', _('Timezone set.'))

        # Wait FOREVER until the user sets his params
        while self.settings.get('user_info_done') is False:
            # wait five seconds and try again
            time.sleep(5)

        # Set user parameters
        username = self.settings.get('username')
        fullname = self.settings.get('fullname')
        password = self.settings.get('password')
        hostname = self.settings.get('hostname')

        sudoers_path = os.path.join(self.dest_dir, "etc/sudoers.d/10-installer")

        with open(sudoers_path, "w") as sudoers:
            sudoers.write('%s ALL=(ALL) ALL\n' % username)

        subprocess.check_call(["chmod", "440", sudoers_path])

        self.queue_event('debug', _('Sudo configuration for user %s done.') % username)

        default_groups = 'lp,video,network,storage,wheel,audio'

        if self.settings.get('require_password') is False:
            default_groups += ',autologin'

        self.chroot(['useradd', '-m', '-s', '/bin/bash', '-g', 'users', '-G', default_groups, username])

        self.queue_event('debug', _('User %s added.') % username)

        self.change_user_password(username, password)

        self.chroot(['chfn', '-f', fullname, username])

        self.chroot(['chown', '-R', '%s:users' % username, "/home/%s" % username])

        hostname_path = os.path.join(self.dest_dir, "etc/hostname")
        with open(hostname_path, "w") as hostname_file:
             hostname_file.write(hostname)

        self.queue_event('debug', _('Hostname  %s set.') % hostname)

        # User password is the root password
        self.change_user_password('root', password)
        self.queue_event('debug', _('Set the same password to root.'))

        # Generate locales
        keyboard_layout = self.settings.get("keyboard_layout")
        keyboard_variant = self.settings.get("keyboard_variant")
        locale = self.settings.get("locale")
        self.queue_event('info', _("Generating locales ..."))

        self.uncomment_locale_gen(locale)

        self.chroot(['locale-gen'])
        locale_conf_path = os.path.join(self.dest_dir, "etc/locale.conf")
        with open(locale_conf_path, "w") as locale_conf:
            locale_conf.write('LANG=%s\n' % locale)
            locale_conf.write('LC_MESSAGES=%s\n' % locale)
            #locale_conf.write('LC_COLLATE=C\n')
            locale_conf.write('LC_COLLATE=%s\n' % locale)

        environment_path = os.path.join(self.dest_dir, "etc/environment")
        with open(environment_path, "w") as environment:
            environment.write('LANG=%s\n' % locale)

        # Set /etc/vconsole.conf
        vconsole_conf_path = os.path.join(self.dest_dir, "etc/vconsole.conf")
        with open(vconsole_conf_path, "w") as vconsole_conf:
            vconsole_conf.write('KEYMAP=%s\n' % keyboard_layout)

        self.queue_event('info', _("Adjusting hardware clock ..."))
        self.auto_timesetting()

        # Enter chroot system
        self.chroot_mount_special_dirs()

        # Install configs for root
        self.chroot(['cp', '-av', '/etc/skel/.', '/root/'])

        self.queue_event('info', _("Configuring hardware ..."))
        # Copy generated xorg.xonf to target
        if os.path.exists("/etc/X11/xorg.conf"):
            shutil.copy2('/etc/X11/xorg.conf', \
                    os.path.join(self.dest_dir, 'etc/X11/xorg.conf'))

        # Configure ALSA
        self.chroot(['sh', '-c', 'amixer -c 0 sset Master 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Front 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Side 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Surround 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Center 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset LFE 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Headphone 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Speaker 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset PCM 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Line 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset External 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset FM 50% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Master Mono 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Master Digital 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Analog Mix 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Aux 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Aux2 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset PCM Center 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset PCM Front 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset PCM LFE 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset PCM Side 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset PCM Surround 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Playback 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset PCM,1 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset DAC 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset DAC,0 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset DAC,1 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Synth 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset CD 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Wave 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Music 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset AC97 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Analog Front 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset VIA DXS,0 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset VIA DXS,1 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset VIA DXS,2 70% unmute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset VIA DXS,3 70% unmute'])

        # set input levels
        self.chroot(['sh', '-c', 'amixer -c 0 sset Mic 70% mute'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset IEC958 70% mute'])

        # special stuff
        self.chroot(['sh', '-c', 'amixer -c 0 sset Master Playback Switch on'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Master Surround on'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset SB Live Analog/Digital Output Jack off'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Audigy Analog/Digital Output Jack off'])

        # special stuff
        self.chroot(['sh', '-c', 'amixer -c 0 sset Master Playback Switch on'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Master Surround on'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset SB Live Analog/Digital Output Jack off'])
        self.chroot(['sh', '-c', 'amixer -c 0 sset Audigy Analog/Digital Output Jack off'])

        # Set pulse
        if os.path.exists("/usr/bin/pulseaudio-ctl"):
            self.chroot(['pulseaudio-ctl', 'normal'])

        # Save settings
        self.chroot(['alsactl', '-f', '/etc/asound.state', 'store'])

        # Exit chroot system
        self.chroot_umount_special_dirs()

        # Install xf86-video driver
        if os.path.exists("/opt/livecd/pacman-gfx.conf"):
            self.queue_event('info', _("Set up graphics card ..."))
            self.queue_event('pulse')
            mhwd_script_path = os.path.join(self.settings.get("thus"), "scripts", MHWS_SCRIPT)
            try:
                subprocess.check_call(["/usr/bin/bash", mhwd_script_path])
                self.queue_event('debug', "Setup graphic card done.")
            except subprocess.FileNotFoundError as e:
                self.queue_fatal_event(_("Can't execute the MHWD script"))
                return False
            except subprocess.CalledProcessError as e:
                self.queue_fatal_event("CalledProcessError.output = %s" % e.output)
                return False

        # Re-enter chroot system
        self.chroot_mount_special_dirs()

        self.queue_event('info', _("Configure display manager ..."))
        # Setup slim
        if os.path.exists("/usr/bin/slim"):
            self.desktop_manager = 'slim'

        # setup lightdm
        if os.path.exists("%s/usr/bin/lightdm" % self.dest_dir):
            self.chroot(['mkdir', '-p', '/run/lightdm'])
            self.chroot(['getent', 'group', 'lightdm'])
            self.chroot(['groupadd', '-g', '620', 'lightdm'])
            self.chroot(['getent', 'passwd', 'lightdm'])
            self.chroot(['useradd', '-c', '"LightDM Display Manager"', \
                         '-u', '620', '-g', 'lightdm', '-d', '/var/run/lightdm', \
                         '-s', '/usr/bin/nologin', 'lightdm'])
            self.chroot(['passwd', '-l', 'lightdm'])
            self.chroot(['chown', '-R', 'lightdm:lightdm', '/run/lightdm'])
            if os.path.exists("%s/usr/bin/startxfce4" % self.dest_dir):
                os.system("sed -i -e 's/^.*user-session=.*/user-session=xfce/' %s/etc/lightdm/lightdm.conf" % self.dest_dir)
                os.system("ln -s /usr/lib/lightdm/lightdm/gdmflexiserver %s/usr/bin/gdmflexiserver" % self.dest_dir)
            os.system("chmod +r %s/etc/lightdm/lightdm.conf" % self.dest_dir)
            self.desktop_manager = 'lightdm'

        # Setup gdm
        if os.path.exists("%s/usr/bin/gdm" % self.dest_dir):
            self.chroot(['getent', 'group', 'gdm'])
            self.chroot(['groupadd', '-g', '120', 'gdm'])
            self.chroot(['getent', 'passwd', 'gdm'])
            self.chroot(['useradd', '-c', '"Gnome Display Manager"', \
                         '-u', '120', '-g', 'gdm', '-d', '/var/lib/gdm', \
                         '-s', '/usr/bin/nologin', 'gdm'])
            self.chroot(['passwd', '-l', 'gdm'])
            self.chroot(['chown', '-R', 'gdm:gdm', '/var/lib/gdm'])
            if os.path.exists("%s/var/lib/AccountsService/users" % self.dest_dir):
                os.system("echo \"[User]\" > %s/var/lib/AccountsService/users/gdm" % self.dest_dir)
                if os.path.exists("%s/usr/bin/startxfce4" % self.dest_dir):
                    os.system("echo \"XSession=xfce\" >> %s/var/lib/AccountsService/users/gdm" % self.dest_dir)
                elif os.path.exists("%s/usr/bin/cinnamon-session" % self.dest_dir):
                    os.system("echo \"XSession=cinnamon-session\" >> %s/var/lib/AccountsService/users/gdm" % self.dest_dir)
                elif os.path.exists("%s/usr/bin/mate-session" % self.dest_dir):
                    os.system("echo \"XSession=mate\" >> %s/var/lib/AccountsService/users/gdm" % self.dest_dir)
                elif os.path.exists("%s/usr/bin/enlightenment_start" % self.dest_dir):
                    os.system("echo \"XSession=enlightenment\" >> %s/var/lib/AccountsService/users/gdm" % self.dest_dir)
                elif os.path.exists("%s/usr/bin/openbox-session" % self.dest_dir):
                    os.system("echo \"XSession=openbox\" >> %s/var/lib/AccountsService/users/gdm" % self.dest_dir)
                elif os.path.exists("%s/usr/bin/lxsession" % self.dest_dir):
                    os.system("echo \"XSession=LXDE\" >> %s/var/lib/AccountsService/users/gdm" % self.dest_dir)
                os.system("echo \"Icon=\" >> %s/var/lib/AccountsService/users/gdm" % self.dest_dir)
            self.desktop_manager = 'gdm'

        # Setup mdm
        if os.path.exists("%s/usr/bin/mdm" % self.dest_dir):
            self.chroot(['getent', 'group', 'mdm'])
            self.chroot(['groupadd', '-g', '128', 'mdm'])
            self.chroot(['getent', 'passwd', 'mdm'])
            self.chroot(['useradd', '-c', '"Linux Mint Display Manager"', \
                         '-u', '128', '-g', 'mdm', '-d', '/var/lib/mdm', \
                         '-s', '/usr/bin/nologin', 'mdm'])
            self.chroot(['passwd', '-l', 'mdm'])
            self.chroot(['chown', 'root:mdm', '/var/lib/mdm'])
            self.chroot(['chmod', '1770', '/var/lib/mdm'])
            if os.path.exists("%s/usr/bin/startxfce4" % self.dest_dir):
                os.system("sed -i 's|default.desktop|xfce.desktop|g' %s/etc/mdm/custom.conf" % self.dest_dir)
            if os.path.exists("%s/usr/bin/cinnamon-session" % self.dest_dir):
                os.system("sed -i 's|default.desktop|cinnamon.desktop|g' %s/etc/mdm/custom.conf" % self.dest_dir)
            if os.path.exists("%s/usr/bin/openbox-session" % self.dest_dir):
                os.system("sed -i 's|default.desktop|openbox.desktop|g' %s/etc/mdm/custom.conf" % self.dest_dir)
            if os.path.exists("%s/usr/bin/mate-session" % self.dest_dir):
                os.system("sed -i 's|default.desktop|mate.desktop|g' %s/etc/mdm/custom.conf" % self.dest_dir)
            if os.path.exists("%s/usr/bin/lxsession" % self.dest_dir):
                os.system("sed -i 's|default.desktop|LXDE.desktop|g' %s/etc/mdm/custom.conf" % self.dest_dir)
            if os.path.exists("%s/usr/bin/enlightenment_start" % self.dest_dir):
                os.system("sed -i 's|default.desktop|enlightenment.desktop|g' %s/etc/mdm/custom.conf" % self.dest_dir)
            self.desktop_manager = 'mdm'

        # Setup lxdm
        if os.path.exists("%s/usr/bin/lxdm" % self.dest_dir):
            self.chroot(['groupadd', '--system', 'lxdm'])
            if os.path.exists("%s/usr/bin/startxfce4" % self.dest_dir):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/startxfce4|' %s/etc/lxdm/lxdm.conf" % self.dest_dir)
            elif os.path.exists("%s/usr/bin/cinnamon-session" % self.dest_dir):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/cinnamon-session|' %s/etc/lxdm/lxdm.conf" % self.dest_dir)
            elif os.path.exists("%s/usr/bin/mate-session" % self.dest_dir):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/mate-session|' %s/etc/lxdm/lxdm.conf" % self.dest_dir)
            elif os.path.exists("%s/usr/bin/enlightenment_start" % self.dest_dir):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/enlightenment_start|' %s/etc/lxdm/lxdm.conf" % self.dest_dir)
            elif os.path.exists("%s/usr/bin/openbox-session" % self.dest_dir):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/openbox-session|' %s/etc/lxdm/lxdm.conf" % self.dest_dir)
            elif os.path.exists("%s/usr/bin/lxsession'" % self.dest_dir):
                os.system("sed -i -e 's|^.*session=.*|session=/usr/bin/lxsession|' %s/etc/lxdm/lxdm.conf" % self.dest_dir)
            os.system("chgrp -R lxdm %s/var/lib/lxdm" % self.dest_dir)
            os.system("chgrp lxdm %s/etc/lxdm/lxdm.conf" % self.dest_dir)
            os.system("chmod +r %s/etc/lxdm/lxdm.conf" % self.dest_dir)
            self.desktop_manager = 'lxdm'

        # Setup kdm
        if os.path.exists("%s/usr/bin/kdm" % self.dest_dir):
            self.chroot(['getent', 'group', 'kdm'])
            self.chroot(['groupadd', '-g', '135', 'kdm'])
            self.chroot(['getent', 'passwd', 'kdm'])
            self.chroot(['useradd', '-u', '135', '-g', 'kdm', '-d', \
                         '/var/lib/kdm', '-s', '/bin/false', '-r', '-M', 'kdm'])
            self.chroot(['chown', '-R', '135:135', 'var/lib/kdm'])
            self.chroot(['xdg-icon-resource', 'forceupdate', '--theme', 'hicolor'])
            self.chroot(['update-desktop-database', '-q'])
            self.desktop_manager = 'kdm'

        self.queue_event('info', _("Configure System ..."))

        # Add BROWSER var
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> %s/etc/environment" % self.dest_dir)
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> %s/etc/skel/.bashrc" % self.dest_dir)
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> %s/etc/profile" % self.dest_dir)
        # Add TERM var
        if os.path.exists("%s/usr/bin/mate-session" % self.dest_dir):
            os.system("echo \"TERM=mate-terminal\" >> %s/etc/environment" % self.dest_dir)
            os.system("echo \"TERM=mate-terminal\" >> %s/etc/profile" % self.dest_dir)

        # Fix_gnome_apps
        self.chroot(['glib-compile-schemas', '/usr/share/glib-2.0/schemas'])
        self.chroot(['gtk-update-icon-cache', '-q', '-t', '-f', '/usr/share/icons/hicolor'])
        self.chroot(['dconf', 'update'])

        if os.path.exists("%s/usr/bin/gnome-keyring-daemon" % self.dest_dir):
            self.chroot(['setcap', 'cap_ipc_lock=ep', '/usr/bin/gnome-keyring-daemon'])

        # Fix_ping_installation
        self.chroot(['setcap', 'cap_net_raw=ep', '/usr/bin/ping'])
        self.chroot(['setcap', 'cap_net_raw=ep', '/usr/bin/ping6'])

        # Remove thus
        if os.path.exists("%s/usr/bin/thus" % self.dest_dir):
            self.queue_event('info', _("Removing live configuration (packages)"))
            self.chroot(['pacman', '-R', '--noconfirm', 'thus'])

        # Remove virtualbox driver on real hardware
        p1 = subprocess.Popen(["mhwd"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["grep","0300:80ee:beef"], stdin=p1.stdout, stdout=subprocess.PIPE)
        num_res = p2.communicate()[0]
        if num_res == "0":
             self.chroot(['sh', '-c', 'pacman -Rsc --noconfirm $(pacman -Qq | grep virtualbox-guest-modules)'])

        # Set unique machine-id
        self.chroot(['dbus-uuidgen', '--ensure=/etc/machine-id'])
        self.chroot(['dbus-uuidgen', '--ensure=/var/lib/dbus/machine-id'])


        # Setup pacman
        self.queue_event("action", _("Configuring package manager"))
        self.queue_event("pulse")

        # Copy mirror list
        shutil.copy2('/etc/pacman.d/mirrorlist', \
                    os.path.join(self.dest_dir, 'etc/pacman.d/mirrorlist'))

        # Copy random generated keys by pacman-init to target
        if os.path.exists("%s/etc/pacman.d/gnupg" % self.dest_dir):
            os.system("rm -rf %s/etc/pacman.d/gnupg"  % self.dest_dir)
        os.system("cp -a /etc/pacman.d/gnupg %s/etc/pacman.d/"  % self.dest_dir)
        self.chroot(['pacman-key', '--populate', 'archlinux', 'manjaro'])
        self.queue_event('info', _("Finished configuring package manager."))

        consolefh = open("%s/etc/keyboard.conf" % self.dest_dir, "r")
        newconsolefh = open("%s/etc/keyboard.new" % self.dest_dir, "w")
        for line in consolefh:
            line = line.rstrip("\r\n")
            if(line.startswith("XKBLAYOUT=")):
                newconsolefh.write("XKBLAYOUT=\"%s\"\n" % keyboard_layout)
            elif(line.startswith("XKBVARIANT=") and keyboard_variant != ''):
                newconsolefh.write("XKBVARIANT=\"%s\"\n" % keyboard_variant)
            else:
                newconsolefh.write("%s\n" % line)
        consolefh.close()
        newconsolefh.close()
        self.chroot(['mv', '/etc/keyboard.conf', '/etc/keyboard.conf.old'])
        self.chroot(['mv', '/etc/keyboard.new', '/etc/keyboard.conf'])

        # Exit chroot system
        self.chroot_umount_special_dirs()

        # Let's start without using hwdetect for mkinitcpio.conf.
        # I think it should work out of the box most of the time.
        # This way we don't have to fix deprecated hooks.
        # NOTE: With LUKS or LVM maybe we'll have to fix deprecated hooks.
        self.queue_event('info', _("Running mkinitcpio ..."))
        self.queue_event("pulse")
        self.run_mkinitcpio()
        self.queue_event('info', _("Running mkinitcpio - done"))

        '''# Call post-install script to execute gsettings commands
        script_path_postinstall = os.path.join(self.settings.get("thus"), \
            "scripts", _postinstall_script)
        subprocess.check_call(["/usr/bin/bash", script_path_postinstall, \
            username, self.dest_dir, self.desktop, keyboard_layout, keyboard_variant])'''

        # Set autologin if selected
        # Warning: In openbox "desktop", the post-install script writes /etc/slim.conf
        # so we always have to call set_autologin AFTER the post-install script call.
        if self.settings.get('require_password') is False:
            self.set_autologin()

        # Encrypt user's home directory if requested (NOT FINISHED YET)
        if self.settings.get('encrypt_home'):
            self.queue_event('debug', _("Encrypting user home dir ..."))
            encfs.setup(username, self.dest_dir)
            self.queue_event('debug', _("User home dir encrypted"))

