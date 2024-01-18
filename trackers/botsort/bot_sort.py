import cv2
import matplotlib.pyplot as plt
import numpy as np
from collections import deque
import os
import os.path as osp
import copy
import torch
import torch.nn.functional as F
from typing import List, Tuple, Dict
from yolov8.ultralytics.yolo.utils.ops import xywh2xyxy, xyxy2xywh

from trackers.botsort import  matching
from trackers.botsort.gmc import GMC
from trackers.botsort.basetrack import BaseTrack, TrackState
from trackers.botsort.kalman_filter import KalmanFilter

# from fast_reid.fast_reid_interfece import FastReIDInterface

from reid_multibackend import ReIDDetectMultiBackend
from yolov8.ultralytics.yolo.utils.ops import xyxy2xywh, xywh2xyxy
import time
import datetime
IMG_WIDTH = 960
IMG_HEIGHT = 540
FOCAL_LENGTH = 480.0

class STrack(BaseTrack):
    current_yaw = 0;
    current_yaw_dot = 0;
    current_yaw_dot_filtered = 0;
    current_depth_image = None
    yaw_dot_list = deque(maxlen=2)
    shared_kalman = KalmanFilter(IMG_WIDTH, IMG_HEIGHT, FOCAL_LENGTH)

    def __init__(self, tlwh, score, cls, feat=None, feat_history=50):

        # wait activate
        self._tlwh = np.asarray(tlwh, dtype=np.float32)
        self.kalman_filter = None
        self.mean, self.covariance = None, None
        self.is_activated = False

        self.cls = -1
        self.cls_hist = []  # (cls id, freq)
        self.update_cls(cls, score)
        #define a mean_history list with max length 10
        self.mean_history = deque(maxlen=3)
        self.bb_depth = None

        self.score = score
        self.tracklet_len = 0

        self.smooth_feat = None
        self.curr_feat = None
        if feat is not None:
            self.update_features(feat)
        self.features = deque([], maxlen=feat_history)
        self.alpha = 0.9

    def get_d1(self):
        #calculate the depth of the object in the depth image
        #get the bounding box of the object in the depth image
        #get the median of the depth values in the bounding box excluding the zeros and the nans
        #return the depth value
        if self.state != TrackState.Tracked and self.state != TrackState.New and self.bb_depth is not None:
            return self.bb_depth
        bounding_box = copy.deepcopy(self.tlwh)
        #get the depth of the bounding box in the depth image
        #clip the bounding box to the image size and remove the negative values
        bounding_box[bounding_box < 0] = 0
        bounding_box[np.isnan(bounding_box)] = 0
        #if any of the bounding values is inf then set it to zero
        # bounding_box[np.isinf(bounding_box)] = 0
        track_depth = copy.deepcopy(STrack.current_depth_image)[int(bounding_box[1]):int(bounding_box[1]+bounding_box[3]), int(bounding_box[0]):int(bounding_box[0]+bounding_box[2])]
        #get the median of the depth values in the bounding box excluding the zeros and the nans
        track_depth = track_depth[track_depth != 0]
        track_depth = track_depth[~np.isnan(track_depth)]
        if len(track_depth) == 0:
            return 0
        self.bb_depth = np.median(track_depth)
        return self.bb_depth
    

    def update_depth_image(depth_image):
        #convert depth image type to float32
        depth_image = depth_image.astype(np.float32)
        depth_image/=10
        STrack.current_depth_image = depth_image

    def update_ego_motion(odom, fps_rot, fps_depth):
        quat = False
        if quat:
            quaternion = (odom.pose.pose.orientation.x, odom.pose.pose.orientation.y,
                    odom.pose.pose.orientation.z, odom.pose.pose.orientation.w)
            #get the yaw from the quaternion not using the tf library
            yaw = np.arctan2(2.0 * (quaternion[3] * quaternion[2] + quaternion[0] * quaternion[1]),
                            1.0 - 2.0 * (quaternion[1] * quaternion[1] + quaternion[2] * quaternion[2]))
        else:
            yaw = odom.pose.pose.orientation.z
        while  abs(yaw-STrack.current_yaw) > np.pi :
            if yaw < STrack.current_yaw :
                yaw += 2*np.pi
            else:
                yaw -= 2*np.pi
        twist = odom.twist.twist
        STrack.current_yaw_dot = twist.angular.z / fps_rot # frames are being published at 20Hz in the simulator
        STrack.yaw_dot_list.append(STrack.current_yaw_dot)
        STrack.current_yaw = yaw
        STrack.current_yaw_dot_filtered = np.mean(STrack.yaw_dot_list)
        raw_x_dot = twist.linear.x
        raw_y_dot = twist.linear.y
        x_dot = raw_x_dot*np.cos(STrack.current_yaw)+raw_y_dot*np.sin(STrack.current_yaw)
        y_dot = -raw_x_dot*np.sin(STrack.current_yaw)+raw_y_dot*np.cos(STrack.current_yaw)
        STrack.current_D_dot = x_dot / fps_depth
        # print("current y_dot is ", y_dot, "   current_x_dot is ", x_dot)

    def update_features(self, feat):
        feat /= np.linalg.norm(feat)
        self.curr_feat = feat
        if self.smooth_feat is None:
            self.smooth_feat = feat
        else:
            self.smooth_feat = self.alpha * self.smooth_feat + (1 - self.alpha) * feat
        self.features.append(feat)
        self.smooth_feat /= np.linalg.norm(self.smooth_feat)

    def update_cls(self, cls, score):
        if len(self.cls_hist) > 0:
            max_freq = 0
            found = False
            for c in self.cls_hist:
                if cls == c[0]:
                    c[1] += score
                    found = True

                if c[1] > max_freq:
                    max_freq = c[1]
                    self.cls = c[0]
            if not found:
                self.cls_hist.append([cls, score])
                self.cls = cls
        else:
            self.cls_hist.append([cls, score])
            self.cls = cls

    def predict(self):
        mean_state = self.mean.copy()
        if self.state != TrackState.Tracked:
            mean_state[6] = 0
            mean_state[7] = 0

        self.mean, self.covariance = self.kalman_filter.predict(mean_state, self.covariance, np.array([STrack.current_yaw_dot]))


    @staticmethod
    def multi_predict(stracks):
        if len(stracks) > 0:
            multi_mean = np.asarray([st.mean.copy() for st in stracks])
            multi_covariance = np.asarray([st.covariance for st in stracks])
            for i, st in enumerate(stracks):
                if st.state != TrackState.Tracked:
                    multi_mean[i][6] = 0
                    multi_mean[i][7] = 0
            control_input = np.array([[STrack.current_yaw_dot, STrack.current_D_dot, st.get_d1() ]for st in stracks])
            multi_mean, multi_covariance = STrack.shared_kalman.multi_predict(multi_mean, multi_covariance, control_input)

            for i, (mean, cov) in enumerate(zip(multi_mean, multi_covariance)):
                stracks[i].mean = mean
                stracks[i].covariance = cov

    @staticmethod
    def multi_gmc(stracks, H=np.eye(2, 3)):
        if len(stracks) > 0:
            multi_mean = np.asarray([st.mean.copy() for st in stracks])
            multi_covariance = np.asarray([st.covariance for st in stracks])

            R = H[:2, :2]
            R8x8 = np.kron(np.eye(4, dtype=float), R)
            t = H[:2, 2]

            for i, (mean, cov) in enumerate(zip(multi_mean, multi_covariance)):
                mean = R8x8.dot(mean)
                mean[:2] += t
                cov = R8x8.dot(cov).dot(R8x8.transpose())

                stracks[i].mean = mean
                stracks[i].covariance = cov

    def activate(self, kalman_filter, frame_id):
        """Start a new tracklet"""
        self.kalman_filter = kalman_filter
        self.track_id = self.next_id()

        self.mean, self.covariance = self.kalman_filter.initiate(self.tlwh_to_xywh(self._tlwh))

        self.tracklet_len = 0
        self.state = TrackState.Tracked
        if frame_id == 1:
            self.is_activated = True
        self.frame_id = frame_id
        self.start_frame = frame_id

    def re_activate(self, new_track, frame_id, new_id=False):

        self.mean, self.covariance = self.kalman_filter.update(self.mean, self.covariance, self.tlwh_to_xywh(new_track.tlwh))
        if new_track.curr_feat is not None:
            self.update_features(new_track.curr_feat)
        self.tracklet_len = 0
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        if new_id:
            self.track_id = self.next_id()
        self.score = new_track.score

        self.update_cls(new_track.cls, new_track.score)

    def update(self, new_track, frame_id):
        """
        Update a matched track
        :type new_track: STrack
        :type frame_id: int
        :type update_feature: bool
        :return:
        """
        self.frame_id = frame_id
        self.tracklet_len += 1

        new_tlwh = new_track.tlwh

        self.mean, self.covariance = self.kalman_filter.update(self.mean, self.covariance, self.tlwh_to_xywh(new_tlwh))

        if new_track.curr_feat is not None:
            self.update_features(new_track.curr_feat)

        self.state = TrackState.Tracked
        self.is_activated = True

        self.score = new_track.score
        self.update_cls(new_track.cls, new_track.score)

    @property
    def tlwh(self):
        """Get current position in bounding box format `(top left x, top left y,
                width, height)`.
        """
        if self.mean is None:
            return self._tlwh.copy()
        ret = self.mean[:4].copy()
        ret[:2] -= ret[2:] / 2
        return ret

    @property
    def tlbr(self):
        """Convert bounding box to format `(min x, min y, max x, max y)`, i.e.,
        `(top left, bottom right)`.
        """
        ret = self.tlwh.copy()
        ret[2:] += ret[:2]
        return ret

    @property
    def xywh(self):
        """Convert bounding box to format `(min x, min y, max x, max y)`, i.e.,
        `(top left, bottom right)`.
        """
        ret = self.tlwh.copy()
        ret[:2] += ret[2:] / 2.0
        return ret

    @staticmethod
    def tlwh_to_xyah(tlwh):
        """Convert bounding box to format `(center x, center y, aspect ratio,
        height)`, where the aspect ratio is `width / height`.
        """
        ret = np.asarray(tlwh).copy()
        ret[:2] += ret[2:] / 2
        ret[2] /= ret[3]
        return ret

    @staticmethod
    def tlwh_to_xywh(tlwh):
        """Convert bounding box to format `(center x, center y, width,
        height)`.
        """
        ret = np.asarray(tlwh).copy()
        ret[:2] += ret[2:] / 2
        return ret

    def to_xywh(self):
        return self.tlwh_to_xywh(self.tlwh)

    @staticmethod
    def tlbr_to_tlwh(tlbr):
        ret = np.asarray(tlbr).copy()
        ret[2:] -= ret[:2]
        return ret

    @staticmethod
    def tlwh_to_tlbr(tlwh):
        ret = np.asarray(tlwh).copy()
        ret[2:] += ret[:2]
        return ret

    def __repr__(self):
        return 'OT_{}_({}-{})'.format(self.track_id, self.start_frame, self.end_frame)


class BoTSORT(object):
    def __init__(self, 
                model_weights,
                device,
                fp16,
                track_high_thresh:float = 0.45,
                new_track_thresh:float = 0.6,
                track_buffer:int = 30,
                match_thresh:float = 0.8,
                proximity_thresh:float = 0.5,
                appearance_thresh:float = 0.25,
                cmc_method:str = 'sparseOptFlow',
                frame_rate=30,
                lambda_=0.985
                ):
        self.last_time_stamp = 0 #time in seconds
        self.tracked_stracks = []  # type: list[STrack]
        self.lost_stracks = []  # type: list[STrack]
        self.removed_stracks = []  # type: list[STrack]
        BaseTrack.clear_count()

        self.frame_id = 0

        self.lambda_ = lambda_
        self.track_high_thresh = track_high_thresh
        self.new_track_thresh = new_track_thresh

        self.buffer_size = int(frame_rate / 30.0 * track_buffer)
        self.max_time_lost = self.buffer_size
        self.kalman_filter = KalmanFilter(IMG_WIDTH, IMG_HEIGHT, FOCAL_LENGTH) #TODO check the parameters
        self.use_depth = True
        self.use_odometry = True
        self.time_window_list = deque(maxlen=300)
        self.time_window_list.append(30)
        self.last_time = time.time()
        # ReID module
        self.proximity_thresh = proximity_thresh
        self.appearance_thresh = appearance_thresh
        self.match_thresh = match_thresh

        self.model = ReIDDetectMultiBackend(weights=model_weights, device=device, fp16=fp16)

        self.gmc = GMC(method=cmc_method, verbose=[None,False])

    def get_all_track_predictions(self):
        """
        Get the predictions of all the active tracks
        :return: list of bounding boxes of all the active tracks
        """
        bboxes = []
        for track in joint_stracks(self.tracked_stracks, self.lost_stracks):
            bbox = track.tlwh
            #append the track id to the bbox
            bbox = np.append(bbox, track.track_id)
            bboxes.append(bbox)
        return bboxes
    
    def update_time(self, odom):
        current_time = odom.header.stamp.to_time()
        time_now = current_time
        if time_now - self.last_time_stamp == 0:
            self.fps = 25
            self.fps_depth = 25
            print(f"odom header time stamp at {self.frame_id} is the same as the last time stamp")
        else:
            self.fps =  (1.0/(time_now - self.last_time_stamp))*1
            self.fps_depth = (1.0/(time_now - self.last_time_stamp)) *1 #TODO This is the fps for translational motion (depth) only
            # print(f'at frame {self.frame_id} fps is {self.fps}')

        self.last_time_stamp = time_now
        # print("fps: ", self.fps)

    def update(self, output_results, img, depth_image = None, odom = None, masks = None):
        self.update_time(odom)
        STrack.update_ego_motion(odom, self.fps, self.fps_depth)
        STrack.update_depth_image(depth_image)
        #get the current time and compare it with the last time update was called
        time_now = time.time()
        self.time_window_list.append(1.0/(time_now - self.last_time))
        #print the average and standard deviation of the time window
        # if self.frame_id % 100 == 0:
        #     print("Average fps: ", np.mean(self.time_window_list), "Standard deviation: ", np.std(self.time_window_list))
        # print((self.fps/2 - self.time_window_list[-1]))
        self.last_time = time_now
        self.frame_id += 1
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []
        
        xyxys = output_results[:, 0:4]
        xywh = xyxy2xywh(xyxys.numpy())
        confs = output_results[:, 4]
        clss = output_results[:, 5]
        
        classes = clss.numpy()
        xyxys = xyxys.numpy()
        confs = confs.numpy()

        remain_inds = confs > self.track_high_thresh
        inds_low = confs > 0.1
        inds_high = confs < self.track_high_thresh

        inds_second = np.logical_and(inds_low, inds_high)
        
        dets_second = xywh[inds_second]
        dets = xywh[remain_inds]
        
        scores_keep = confs[remain_inds]
        scores_second = confs[inds_second]
        
        classes_keep = classes[remain_inds]
        clss_second = classes[inds_second]

        self.height, self.width = img.shape[:2]

        '''Extract embeddings '''
        features_keep = self._get_features(dets, img)

        if len(dets) > 0:
            '''Detections'''
            
            detections = [STrack(xyxy, s, c, f.cpu().numpy()) for
                              (xyxy, s, c, f) in zip(dets, scores_keep, classes_keep, features_keep)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with high score detection boxes'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks)

        # Predict the current location with KF
        STrack.multi_predict(strack_pool)

        # Fix camera motion
        warp = self.gmc.apply(img, dets)
        STrack.multi_gmc(strack_pool, warp)
        STrack.multi_gmc(unconfirmed, warp)

        # Associate with high score detection boxes
        raw_emb_dists = matching.embedding_distance(strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter, raw_emb_dists, strack_pool, detections, only_position=False, lambda_=self.lambda_)

        # ious_dists = matching.iou_distance(strack_pool, detections)
        # ious_dists_mask = (ious_dists > self.proximity_thresh)

        # ious_dists = matching.fuse_score(ious_dists, detections)

        # emb_dists = matching.embedding_distance(strack_pool, detections) / 2.0
        # raw_emb_dists = emb_dists.copy()
        # emb_dists[emb_dists > self.appearance_thresh] = 1.0
        # emb_dists[ious_dists_mask] = 1.0
        # dists = np.minimum(ious_dists, emb_dists)

            # Popular ReID method (JDE / FairMOT)
            # raw_emb_dists = matching.embedding_distance(strack_pool, detections)
            # dists = matching.fuse_motion(self.kalman_filter, raw_emb_dists, strack_pool, detections)
            # emb_dists = dists

            # IoU making ReID
            # dists = matching.embedding_distance(strack_pool, detections)
            # dists[ious_dists_mask] = 1.0
    
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=self.match_thresh)

        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        ''' Step 3: Second association, with low score detection boxes'''
        # if len(scores):
        #     inds_high = scores < self.track_high_thresh
        #     inds_low = scores > self.track_low_thresh
        #     inds_second = np.logical_and(inds_low, inds_high)
        #     dets_second = bboxes[inds_second]
        #     scores_second = scores[inds_second]
        #     classes_second = classes[inds_second]
        # else:
        #     dets_second = []
        #     scores_second = []
        #     classes_second = []

        # association the untrack to the low score detections
        if len(dets_second) > 0:
            '''Detections'''
            detections_second = [STrack(STrack.tlbr_to_tlwh(tlbr), s, c) for
                (tlbr, s, c) in zip(dets_second, scores_second, clss_second)]
        else:
            detections_second = []

        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections_second)
        matches, u_track, u_detection_second = matching.linear_assignment(dists, thresh=0.5)
        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections_second[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        detections = [detections[i] for i in u_detection]
        ious_dists = matching.iou_distance(unconfirmed, detections)
        ious_dists_mask = (ious_dists > self.proximity_thresh)
        
        ious_dists = matching.fuse_score(ious_dists, detections)
    
        emb_dists = matching.embedding_distance(unconfirmed, detections) / 2.0
        raw_emb_dists = emb_dists.copy()
        emb_dists[emb_dists > self.appearance_thresh] = 1.0
        emb_dists[ious_dists_mask] = 1.0
        dists = np.minimum(ious_dists, emb_dists)
    
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            unconfirmed[itracked].update(detections[idet], self.frame_id)
            activated_starcks.append(unconfirmed[itracked])
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.new_track_thresh:
                continue

            track.activate(self.kalman_filter, self.frame_id)
            activated_starcks.append(track)

        """ Step 5: Update state"""
        for track in self.lost_stracks:
            if self.frame_id - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        """ Merge """
        self.tracked_stracks = [t for t in self.tracked_stracks if t.state == TrackState.Tracked]
        self.tracked_stracks = joint_stracks(self.tracked_stracks, activated_starcks)
        self.tracked_stracks = joint_stracks(self.tracked_stracks, refind_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.tracked_stracks)
        self.lost_stracks.extend(lost_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.removed_stracks)
        self.removed_stracks.extend(removed_stracks)
        self.tracked_stracks, self.lost_stracks = remove_duplicate_stracks(self.tracked_stracks, self.lost_stracks)

        # output_stracks = [track for track in self.tracked_stracks if track.is_activated]
        output_stracks = [track for track in self.tracked_stracks if track.is_activated]
        outputs = []
        for t in output_stracks:
            output= []
            tlwh = t.tlwh
            tid = t.track_id
            tlwh = np.expand_dims(tlwh, axis=0)
            xyxy = xywh2xyxy(tlwh)
            xyxy = np.squeeze(xyxy, axis=0)
            output.extend(xyxy)
            output.append(tid)
            output.append(t.cls)
            output.append(t.score)
            outputs.append(output)

        return outputs

    def _xywh_to_xyxy(self, bbox_xywh):
        x, y, w, h = bbox_xywh
        x1 = max(int(x - w / 2), 0)
        x2 = min(int(x + w / 2), self.width - 1)
        y1 = max(int(y - h / 2), 0)
        y2 = min(int(y + h / 2), self.height - 1)
        return x1, y1, x2, y2

    def _get_features(self, bbox_xywh, ori_img):
        im_crops = []
        for box in bbox_xywh:
            x1, y1, x2, y2 = self._xywh_to_xyxy(box)
            im = ori_img[y1:y2, x1:x2]
            im_crops.append(im)
        if im_crops:
            features = self.model(im_crops)
        else:
            features = np.array([])
        return features

def joint_stracks(tlista, tlistb):
    exists = {}
    res = []
    for t in tlista:
        exists[t.track_id] = 1
        res.append(t)
    for t in tlistb:
        tid = t.track_id
        if not exists.get(tid, 0):
            exists[tid] = 1
            res.append(t)
    return res


def sub_stracks(tlista, tlistb):
    stracks = {}
    for t in tlista:
        stracks[t.track_id] = t
    for t in tlistb:
        tid = t.track_id
        if stracks.get(tid, 0):
            del stracks[tid]
    return list(stracks.values())


def remove_duplicate_stracks(stracksa, stracksb):
    pdist = matching.iou_distance(stracksa, stracksb)
    pairs = np.where(pdist < 0.15)
    dupa, dupb = list(), list()
    for p, q in zip(*pairs):
        timep = stracksa[p].frame_id - stracksa[p].start_frame
        timeq = stracksb[q].frame_id - stracksb[q].start_frame
        if timep > timeq:
            dupb.append(q)
        else:
            dupa.append(p)
    resa = [t for i, t in enumerate(stracksa) if not i in dupa]
    resb = [t for i, t in enumerate(stracksb) if not i in dupb]
    return resa, resb
