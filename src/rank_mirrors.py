#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  rank_mirrors.py
#  
#  Copyright 2013 Manjaro
#  Copyright 2013 Cinnarch
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
#  Manjaro Team:
#   Roland Singer (singro)   <roland.manjaro.org>
#   Philip Müller (philm)    <philm.manjaro.org>
#   Guillaume Benoit (guinux)<guillaume.manjaro.org>
#  
#  Cinnarch Team:
#   Alex Filgueira (faidoc) <alexfilgueira.cinnarch.com>
#   Raúl Granados (pollitux) <raulgranados.cinnarch.com>
#   Gustau Castells (karasu) <karasu.cinnarch.com>
#   Kirill Omelchenko (omelcheck) <omelchek.cinnarch.com>
#   Marc Miralles (arcnexus) <arcnexus.cinnarch.com>
#   Alex Skinner (skinner) <skinner.cinnarch.com>

import threading
import multiprocessing
import subprocess

NM = 'org.freedesktop.NetworkManager'
NM_STATE_CONNECTED_GLOBAL = 70
        
class AutoRankmirrorsThread(threading.Thread):
    def __init__(self):
        super(AutoRankmirrorsThread, self).__init__()

    def get_prop(self, obj, iface, prop):
        import dbus
        try:
            return obj.Get(iface, prop, dbus_interface=dbus.PROPERTIES_IFACE)
        except dbus.DBusException as e:
            if e.get_dbus_name() == 'org.freedesktop.DBus.Error.UnknownMethod':
                return None
            else:
                raise
        
    def has_connection(self):
        import dbus
        try:
            bus = dbus.SystemBus()
            manager = bus.get_object(NM, '/org/freedesktop/NetworkManager')
            state = self.get_prop(manager, NM, 'state')
        except dbus.exceptions.DBusException:
            log.debug(_("In rankmirrors, can't get network status"))
            return False
        return state == NM_STATE_CONNECTED_GLOBAL

    def run(self):
        # wait until there is an Internet connection available
        while not self.has_connection():
            time.sleep(2)  # Delay 
            if self.stop_event.is_set():
                #self.coords_queue.clear()
                return

        # Run rankmirrors command
        try:
            subprocess.check_call(['/bin/bash', '/usr/share/thus/scripts/rankmirrors.sh'])
        except subprocess.CalledProcessError as e:
            print(_("Couldn't execute auto mirroring selection"))
        
