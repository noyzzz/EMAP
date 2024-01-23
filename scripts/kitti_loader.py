# Ultralytics YOLO 🚀, GPL-3.0 license
from __future__ import print_function

import glob
import math
import os
import time
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import cv2
import numpy as np
import torch
from PIL import Image

import sys
sys.path.append("../")
from ros_classes import image_converter

import roslib
import sys
import rospy
from std_msgs.msg import String
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
from collections import namedtuple


from ultralytics.yolo.data.augment import LetterBox
from ultralytics.yolo.data.utils import IMG_FORMATS, VID_FORMATS
from ultralytics.yolo.utils import LOGGER, ROOT, is_colab, is_kaggle, ops
from ultralytics.yolo.utils.checks import check_requirements
import threading
import kitti_loader_utils as utils
class KittiLoader:
    def __init__(self, kitii_base_path: str, sequence: str, transform = None, 
                    focalx = 320.0, focaly = 320.0, centerx = 320.0, centery = 240.0):
            self.base_path = kitii_base_path
            self.sequence = sequence

            # Find all the data files
            self._get_file_lists()
            self._load_calib()
            self._load_oxts() # it loads all the oxts data for a sequence. not an effiecient way to load oxts data
            self.transform = transform
    
    def _get_file_lists(self):
        """Find and list data files for each sensor."""
        self.cam2_files = sorted(glob.glob(
            os.path.join(self.base_path,
                         'image_02',
                         self.sequence,
                         '*.{}'.format(self.imtype))))
        self.cam3_files = sorted(glob.glob(
            os.path.join(self.base_path,
                         'image_03',
                         self.sequence,
                         '*.{}'.format(self.imtype))))
        self.velo_files = sorted(glob.glob(
            os.path.join(self.base_path,
                        'velodyne',
                        self.sequence,
                         '*.bin')))
        
        self.oxts_files = sorted(glob.glob(
            os.path.join(self.base_path,
                        'oxts',
                         f'{self.sequence}.txt')))
    def _load_calib(self):
        """Load and compute intrinsic and extrinsic calibration parameters."""
        # We'll build the calibration parameters as a dictionary, then
        # convert it to a namedtuple to prevent it from being modified later
        data = {}

        # Load the calibration file
        calib_filepath = os.path.join(self.base_path,'calib', f'{self.sequence}.txt')
        filedata = utils.read_calib_file(calib_filepath)

        # Create 3x4 projection matrices
        P_rect_00 = np.reshape(filedata['P0'], (3, 4))
        P_rect_10 = np.reshape(filedata['P1'], (3, 4))
        P_rect_20 = np.reshape(filedata['P2'], (3, 4))
        P_rect_30 = np.reshape(filedata['P3'], (3, 4))

        data['P_rect_00'] = P_rect_00
        data['P_rect_10'] = P_rect_10
        data['P_rect_20'] = P_rect_20
        data['P_rect_30'] = P_rect_30

        # Compute the rectified extrinsics from cam0 to camN
        T1 = np.eye(4)
        T1[0, 3] = P_rect_10[0, 3] / P_rect_10[0, 0]
        T2 = np.eye(4)
        T2[0, 3] = P_rect_20[0, 3] / P_rect_20[0, 0]
        T3 = np.eye(4)
        T3[0, 3] = P_rect_30[0, 3] / P_rect_30[0, 0]

        Tr_velo_cam = np.reshape(filedata['Tr_velo_cam'], (3, 4))
        data['Tr_velo_cam'] = np.vstack([Tr_velo_cam, [0, 0, 0, 1]])

        Tr_imu_velo = np.reshape(filedata['Tr_imu_velo'], (3, 4))
        data['Tr_imu_velo'] = np.vstack([Tr_imu_velo, [0, 0, 0, 1]])

        # Compute the camera intrinsics
        data['K_cam0'] = P_rect_00[0:3, 0:3]
        data['K_cam1'] = P_rect_10[0:3, 0:3]
        data['K_cam2'] = P_rect_20[0:3, 0:3]
        data['K_cam3'] = P_rect_30[0:3, 0:3]

        self._calib = namedtuple('CalibData', data.keys())(*data.values())
    
    @property
    def calib(self):
        """Return a namedtuple of calibration parameters."""
        return self._calib
    
    
    
    def _load_oxts(self):
        """Load OXTS data from file."""
        self.oxts = utils.load_oxts_packets_and_poses(self.oxts_files)
        """Generator to read OXTS ground truth data.

           Poses are given in an East-North-Up coordinate system 
           whose origin is the first GPS position.
        """
        # Scale for Mercator projection (from first lat value)
        scale = None
        # Origin of the global coordinate system (first GPS position)
        origin = None

        oxts = []

        for filename in self.oxts_files:
            with open(filename, 'r') as f:
                for line in f.readlines():
                    line = line.split()
                    # Last five entries are flags and counts
                    line[:-5] = [float(x) for x in line[:-5]]
                    line[-5:] = [int(float(x)) for x in line[-5:]]

                    packet = utils.OxtsPacket(*line)

                    if scale is None:
                        scale = np.cos(packet.lat * np.pi / 180.)

                    R, t = utils.pose_from_oxts_packet(packet, scale)

                    if origin is None:
                        origin = t

                    T_w_imu = utils.transform_from_rot_trans(R, t - origin)

                    oxts.append(utils.OxtsData(packet, T_w_imu))

        self.oxts = oxts
            
    def __getitem__(self, index):
        """Return the data from a particular index."""
        # Load the data from disk
        cam2_0 = cv2.imread(self.cam2_files[index].strip())
        velo = np.fromfile(self.velo_files[index].strip(), dtype=np.float32)
        oxt = self.oxts[index]

        # Apply the data transformations
        if self.transform is not None:
            cam2 = self.transform(cam2)
        else:
            cam2 = LetterBox(self.imgsz, self.auto, stride=self.stride)(image=cam2_0)
            cam2 = cam2_0.transpose((2, 0, 1))[::-1]  # HWC to CHW, BGR to RGB
            cam2 = np.ascontiguousarray(cam2_0)  # contiguous
        
        self.extra_output = { "velodyne": velo, "oxt": oxt, "gt": self.gt} #TODO: add gt

        return self.base_path, cam2, cam2_0, "", self.extra_output
    
    def __next__(self):
        """Return the next sequence."""
        # Get the data from the next index
        data = self.__getitem__(self.index)

        # Increment the index and loop if necessary
        self.index += 1
        if self.index >= len(self):
            raise StopIteration

        return data
    
    def __len__(self):
        """Return the number of frames loaded."""
        return len(self.cam2_files)
    

if __name__ == "__main__":
    dataset = KittiLoader("/home/apera/mhmd/kittiMOT/data_kittiMOT/training", "0", transform = None)