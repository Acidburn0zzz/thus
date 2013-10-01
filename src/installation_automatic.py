#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  installation_automatic.py
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

import xml.etree.ElementTree as etree

from gi.repository import Gtk
import subprocess
import os
import sys
import misc
import logging
import installation_process

# To be able to test this installer in other systems
# that do not have pyparted3 installed
try:
    import parted
except:
    print("Can't import parted module! This installer won't work.")

_next_page = "timezone"
_prev_page = "installation_ask"

class InstallationAutomatic(Gtk.Box):

    def __init__(self, params):
        self.title = params['title']
        self.ui_dir = params['ui_dir']
        self.forward_button = params['forward_button']
        self.backwards_button = params['backwards_button']
        self.callback_queue = params['callback_queue']
        self.settings = params['settings']
        self.alternate_package_list = params['alternate_package_list']
        
        super().__init__()
        self.ui = Gtk.Builder()
        self.ui.add_from_file(os.path.join(self.ui_dir, "installation_automatic.ui"))

        self.ui.connect_signals(self)

        self.device_store = self.ui.get_object('part_auto_select_drive')

        self.device_label = self.ui.get_object('part_auto_select_drive_label')

        super().add(self.ui.get_object("installation_automatic"))

        self.devices = dict()
        self.process = None

    def translate_ui(self):
        txt = _("Automatic installation mode")
        txt = "<span weight='bold' size='large'>%s</span>" % txt
        self.title.set_markup(txt)

        txt = _("Select drive:")
        self.device_label.set_markup(txt)

        label = self.ui.get_object('text_automatic')
        txt = _("WARNING! This installation mode will overwrite everything in your drive!")
        txt = "<b>%s</b>" % txt
        label.set_markup(txt)

        label = self.ui.get_object('text_automatic2')
        txt = _("Please choose the drive where you want to install Manjaro\nand click the button below to start the process.")
        txt = "%s" % txt
        label.set_markup(txt)

        txt = _("Install now!")
        self.forward_button.set_label(txt)

    @misc.raise_privileges
    def populate_devices(self):
        device_list = parted.getAllDevices()
        
        self.device_store.remove_all()
        self.devices = {}
                   
        for dev in device_list:
            ## avoid cdrom and any raid, lvm volumes or encryptfs
            if not dev.path.startswith("/dev/sr") and \
               not dev.path.startswith("/dev/mapper"):
                # hard drives measure themselves assuming kilo=1000, mega=1mil, etc
                size_in_gigabytes = int((dev.length * dev.sectorSize) / 1000000000)
                line = '{0} [{1} GB] ({2})'.format(dev.model, size_in_gigabytes, dev.path)
                self.device_store.append_text(line)
                self.devices[line] = dev.path
                logging.debug(line)

        self.select_first_combobox_item(self.device_store)

    def select_first_combobox_item(self, combobox):
        tree_model = combobox.get_model()
        tree_iter = tree_model.get_iter_first()
        combobox.set_active_iter(tree_iter)

    def on_select_drive_changed(self, widget):
        line = self.device_store.get_active_text()
        if line != None:
            self.auto_device = self.devices[line]
        self.forward_button.set_sensitive(True)

    def prepare(self, direction):
        self.translate_ui()
        self.populate_devices()
        self.show_all()
        #self.forward_button.set_sensitive(False)

    def store_values(self):
        #self.forward_button.set_sensitive(True)
        #installer_settings['auto_device'] = self.auto_device
        logging.info(_("Automatic install using %s device") % self.auto_device)
        self.start_installation()
        return True

    def get_prev_page(self):
        return _prev_page

    def get_next_page(self):
        return _next_page

    def refresh(self):
        while Gtk.events_pending():
            Gtk.main_iteration()

    def start_installation(self):
        #self.install_progress.set_sensitive(True)
        logging.info(_("Manjaro will use %s as installation device") % self.auto_device)

        if self.settings.get('use_lvm'):
            # WARNING! : This must be the same that appears in auto_partition.sh
            root_partition = "/dev/ManjaroVG/ManjaroRoot"
        else:
            root_partition = self.auto_device + "3"

        boot_partition = self.auto_device + "1"
        
        # TODO: UEFI Install (must update auto_partition.sh)
        # root_partition = self.auto_device + "5"
        # boot_partition = self.atuo_device + "3"

        mount_devices = {}
        root_partition = self.auto_device + "3"
        boot_partition = self.auto_device + "1"
        mount_devices["/"] = root_partition 
        mount_devices["/boot"] = boot_partition

        fs_devices = {}
        fs_devices[boot_partition] = "ext2"
        fs_devices[root_partition] = "ext4"

        # Ask bootloader type
        import bootloader
        bl = bootloader.BootLoader(self.settings)
        bl.ask()

        if self.settings.get('install_bootloader'):
            self.settings.set('bootloader_device', self.auto_device)
            logging.info(_("Manjaro will install the bootloader of type %s in %s") % \
                (self.settings.get('bootloader_type'), self.settings.get('bootloader_device')))
        else:
            logging.warning("Thus will not install any boot loader")

        self.process = installation_process.InstallationProcess( \
                        self.settings, \
                        self.callback_queue, \
                        mount_devices, \
                        fs_devices, \
                        None, \
                        self.alternate_package_list)
                        
        self.process.start()
