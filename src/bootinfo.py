#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  bootinfo.py
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

import os

def get_os(mountname):
    #  If partition is mounted, try to identify the Operating System
    # (OS) by looking for files specific to the OS.
    OS = "unknown"

    win_dirs = ["windows", "Windows", "WINDOWS"]
    system_dirs = ["System32", "system32"]
    winload_names = ["Winload.exe", "winload.exe"]
    secevent_names = ["SecEvent.Evt", "secevent.evt"]
    
    vista_mark = "W.i.n.d.o.w.s. .V.i.s.t.a"
    seven_mark = "W.i.n.d.o.w.s. .7"
    
    for windows in win_dirs:
        for system in system_dirs:
            for name in winload_names:
                p = os.path.join(mountname, windows, system, name)
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        if vista_mark in f:
                            OS = "Windows Vista"
                        elif seven_mark in f:
                            OS = "Windows 7"
            if OS == "unknown":
                for name in secevent_names:
                    p = os.path.join(mountname, windows, system, "config", name)
                    if os.path.exists(p):
                        OS = "Windows XP"
    if OS == "unknown":
        p = os.path.join(mountname, "ReactOS/system32/config/SecEvent.Evt")
        if os.path.exists(p):
            OS = "ReactOS"

    dos_marks = ["MS-DOS", "MS-DOS 6.22", "MS-DOS 6.21", "MS-DOS 6.0", \
                 "MS-DOS 5.0", "MS-DOS 4.01", "MS-DOS 3.3", "Windows 98" \
                 "Windows 95"]

    dos_names = ["IO.SYS", "io.sys"]
    
    for name in dos_names:
        p = os.path.join(mountname, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                for mark in dos_marks:
                    if mark in f:
                        OS = mark

    # Linuxes
    
    if OS == "unknown":
        linux_names = ["issue", "slackware_version"]
        for name in linux_names:
            p = os.path.join(mountname, "etc", name)
            if os.path.exists(p):
                with open(p, "rt") as f:
                    t = f.readline()
                textlist = t.split()
                text = ""
                for l in textlist:
                    if not "\\" in l:
                        text = text + l
                OS = text

    return OS

if __name__ == '__main__':
    print(get_os("/"))
                
            
        

'''

[ -s "${mountname}/etc/issue" ] && OS=$(sed -e 's/\\. //g' -e 's/\\.//g' -e 's/^[ \t]*//' "${mountname}"/etc/issue);

[ -s "${mountname}/etc/slackware-version" ] && OS=$(sed -e 's/\\. //g' -e 's/\\.//g' -e 's/^[ \t]*//' "${mountname}"/etc/slackware-version);
'''
    
