#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  installation_ask.py
#  
#  Copyright 2013 Antergos, Manjaro
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
#  
#  Antergos Team:
#   Alex Filgueira (faidoc) <alexfilgueira.antergos.com>
#   Raúl Granados (pollitux) <raulgranados.antergos.com>
#   Gustau Castells (karasu) <karasu.antergos.com>
#   Kirill Omelchenko (omelcheck) <omelchek.antergos.com>
#   Marc Miralles (arcnexus) <arcnexus.antergos.com>
#   Alex Skinner (skinner) <skinner.antergos.com>

from gi.repository import Gtk

import subprocess
import os
import logging
import bootinfo

import config

_prev_page = "features"

class InstallationAsk(Gtk.Box):

    def __init__(self, params):
        self.title = params['title']
        self.ui_dir = params['ui_dir']
        self.forward_button = params['forward_button']
        self.backwards_button = params['backwards_button']
        self.settings = params['settings']
        
        super().__init__()
        self.ui = Gtk.Builder()
        self.ui.add_from_file(os.path.join(self.ui_dir, "installation_ask.ui"))

        partitioner_dir = os.path.join(self.settings.get("DATA_DIR"), "partitioner/")

        image = self.ui.get_object("automatic_image")
        image.set_from_file(partitioner_dir + "automatic.png")

        image = self.ui.get_object("alongside_image")
        image.set_from_file(partitioner_dir + "alongside.png")

        image = self.ui.get_object("advanced_image")
        image.set_from_file(partitioner_dir + "advanced.png")

        self.ui.connect_signals(self)

        super().add(self.ui.get_object("installation_ask"))
        
        oses = {}
        oses = bootinfo.get_os_dict()
        
        self.otherOS = ""
        for k in oses:
            if "sda" in k and oses[k] != "unknown":
                self.otherOS = oses[k]
                
        # by default, select automatic installation
        self.next_page = "installation_automatic"
        
    def prepare(self, direction):
        self.translate_ui()
        self.show_all()
        
        # Hide alongside option if no OS has been detected
        if self.otherOS == "":
            radio = self.ui.get_object("alongside_radiobutton")
            radio.hide()
            label = self.ui.get_object("alongside_description")
            label.hide()

    def translate_ui(self):
        txt = _("Installation type")
        txt = "<span weight='bold' size='large'>%s</span>" % txt
        self.title.set_markup(txt)
        
        # In case we're coming from an installer screen, we change
        # to forward stock button and we activate it
        self.forward_button.set_label("gtk-go-forward")
        self.forward_button.set_sensitive(True)

        radio = self.ui.get_object("automatic_radiobutton")
        radio.set_label(_("Erase disk and install Manjaro (automatic)"))

        label = self.ui.get_object("automatic_description")
        txt = _("Warning: This will delete all data on your disk")
        txt = '<span weight="light" size="small">%s</span>' % txt
        label.set_markup(txt)
        label.set_line_wrap(True)
        
        # alongside is still experimental. Needs a lot of testing.
        radio = self.ui.get_object("alongside_radiobutton")
        radio.set_label(_("Install Manjaro alongside %s") % self.otherOS)

        label = self.ui.get_object("alongside_description")
        txt = _("Install this OS alongside the other OSes you have already installed.")
        txt = '<span weight="light" size="small">%s</span>' % txt
        label.set_markup(txt)
        label.set_line_wrap(True)

        radio = self.ui.get_object("advanced_radiobutton")
        radio.set_label(_("Manage your partitions where to install Manjaro (advanced)"))

        label = self.ui.get_object("advanced_description")
        txt = _("Create/delete partitions, and also choose additional mount points.")
        txt = '<span weight="light" size="small">%s</span>' % txt
        label.set_markup(txt)
        label.set_line_wrap(True)

    def store_values(self):
        if self.next_page == "installation_automatic":
            self.settings.set('partition_mode', 'automatic')
        elif self.next_page == "installation_alongside":
            self.settings.set('partition_mode', 'alongside')
        else:
            self.settings.set('partition_mode', 'advanced')
                
        return True

    def get_next_page(self):
        return self.next_page

    def get_prev_page(self):
        return _prev_page

    def on_automatic_radiobutton_toggled(self, widget):
        if widget.get_active():
            self.next_page = "installation_automatic"

    def on_easy_radiobutton_toggled(self, widget):
        if widget.get_active():
            self.next_page = "installation_alongside"

    def on_advanced_radiobutton_toggled(self, widget):
        if widget.get_active():
            self.next_page = "installation_advanced"
