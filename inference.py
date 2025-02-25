"""
    Input: epoch_{epoch}.tar
    Inference:
        log_dir/dump_{epoch}_{split}
    Evaluate:
        log_dir/dump_{epoch}_{split}/ap_{epoch}_{split}.npy
"""
import os
import sys
import yaml
import time
import torch
import json
import argparse
from PIL import Image
import numpy as np
import open3d as o3d
from tqdm import tqdm
import cv2
from torch.utils.data import DataLoader
# from suctionnetAPI import SuctionNetEval
from graspnetAPI.graspnet_eval import GraspGroup, GraspNetEval
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, ROOT_DIR)
from utils.data_utils import CameraInfo,create_point_cloud_from_depth_image

from models.graspnet import GraspNet, pred_grasp_decode
from dataset.graspnet_dataset import GraspNetDataset, minkowski_collate_fn, load_grasp_labels
from utils.collision_detector import ModelFreeCollisionDetector

parser = argparse.ArgumentParser()
# variable in shell
parser.add_argument('--infer', action='store_true', default=False)
parser.add_argument('--eval', action='store_true', default=False)
parser.add_argument('--log_dir', default=os.path.join(ROOT_DIR + '/logs'), required=False)
# parser.add_argument('--chosen_model', default='1billion.tar', required=False)
parser.add_argument('--collision_thresh', type=float, default=0.01,
                    help='Collision Threshold in collision detection [default: 0.01]')
parser.add_argument('--epoch_range', type=list, default=[79], help='epochs to infer&eval')

# default in shell
parser.add_argument('--batch_size', type=int, default=1, help='Batch Size during inference [default: 1]')
parser.add_argument('--seed_feat_dim', default=256, type=int, help='Point wise feature dim')
parser.add_argument('--camera', default='realsense', help='Camera split [realsense/kinect]')
parser.add_argument('--num_point', type=int, default=15000, help='Point Number [default: 15000]')
parser.add_argument('--voxel_size', type=float, default=0.005, help='Voxel Size for sparse convolution')
parser.add_argument('--voxel_size_cd', type=float, default=0.01, help='Voxel Size for collision detection')
cfgs = parser.parse_args()
DATA_PATH = os.path.join(ROOT_DIR, "example_data")

# load model config
with open(os.path.join(ROOT_DIR + '/models/model_config.yaml'), 'r') as f:
    model_config = yaml.load(f, Loader=yaml.FullLoader)

def process_data(return_raw_cloud = False):
    depth = np.array(Image.open(os.path.join(DATA_PATH, 'depth.png')))
    color = np.array(Image.open(os.path.join(DATA_PATH, 'color.png')),dtype=np.float32) / 255.0

    # with open(os.path.join(ROOT_DIR + '/results.json')) as f:
    #     fil = json.load(f)
    #     labels = fil["Intrinsics_labels"]
    #     values = fil["Intrinsics_values"]
    #     intrinsics = dict(zip(labels, values))
    #     fx = intrinsics.get("fx")
    #     fy = intrinsics.get("fy")
    #     cx = intrinsics.get("cx")
    #     cy = intrinsics.get("cy")
    #     width = intrinsics.get("width")
    #     height = intrinsics.get("height")
    #     #print(f"fx: {fx}, fy: {fy}, cx: {cx}, cy: {cy}, width: {width}, height: {height}")
    

    fx, fy = 927.17, 927.37
    cx, cy = 651.32, 349.62
    width, height = 1280, 720
    factor_depth = 1000
    camera = CameraInfo(width, height, fx, fy, cx, cy, factor_depth)

    # x,y,w,h = 264,47,660,479
    # mask_crop = np.zeros((720,1280),dtype=np.uint8)
    # cv2.rectangle(mask_crop, (x,y), (x+w,y+h),255,cv2.FILLED)
    # color = cv2.bitwise_and(color,color,mask=mask_crop)
    # depth = cv2.bitwise_and(depth,depth,mask=mask_crop)

    #########
    # color = cv2.cvtColor(color, cv2.COLOR_BGR2RGB).astype(np.float32)
    #########

    cloud = create_point_cloud_from_depth_image(depth, camera, organized=True)
    # print("cloud shape:", cloud.shape)
    
    cloud_masked = cloud.reshape(-1,3)
    mask = (cloud_masked[:, 2] >= 0) & (cloud_masked[:, 2] <= 1) # filter out points outside the table
    # print("cloud_masked shape:", cloud_masked.shape)
    cloud_masked = cloud_masked[mask]

    color_masked = color.reshape(-1, color.shape[-1])
    color_masked = color_masked[mask]
    
    if return_raw_cloud:
        return cloud_masked

    if len(cloud_masked) >= cfgs.num_point:
        idxs = np.random.choice(len(cloud_masked), cfgs.num_point, replace=False)
    else:
        idxs1 = np.arange(len(cloud_masked))
        idxs2 = np.random.choice(len(cloud_masked), cfgs.num_point - len(cloud_masked), replace=True)
        idxs = np.concatenate([idxs1, idxs2], axis=0)
    cloud_sampled = cloud_masked[idxs]
    color_sampled = color_masked[idxs]


    # print("Min color:", np.min(color_masked, axis=0))
    # print("Max color:", np.max(color_masked, axis=0))

    # print("cloud_masked shape:", cloud_masked.shape)
    # print("color_masked shape:", color_masked.shape)
    # print("cloud_masked sample:\n", cloud_masked[:5])
    # print("color_masked sample:\n", color_masked[:5])


    
    # cloud_sampled = cloud_masked
    # color_sampled = color_masked

    ret_dict = {
                'raw_point_clouds': cloud_masked.astype(np.float32),
                'raw_color': color_masked.astype(np.float32),
                'point_clouds': cloud_sampled.astype(np.float32),
                'coors': cloud_sampled.astype(np.float32) / cfgs.voxel_size,
                'feats': np.ones_like(cloud_sampled).astype(np.float32),
                'color': color_sampled.astype(np.float32),
                }
    return ret_dict


def inference(chosen_model = '1billion.tar'):
    sample_data = process_data()
    raw_pointclouds = sample_data['raw_point_clouds']
    raw_color = sample_data['raw_color']
    net = GraspNet(model_config, is_training=False)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    net.to(device)
    
    #checkpoint_path = os.path.join(cfgs.log_dir, 'epoch_{}.tar'.format(epoch))
    checkpoint_path = os.path.join(cfgs.log_dir, chosen_model)
    checkpoint = torch.load(checkpoint_path)
    net.load_state_dict(checkpoint['model_state_dict'])
    sample_data = minkowski_collate_fn([sample_data])
    for key in sample_data:
        if 'list' in key:
            for i in range(len(sample_data[key])):
                for j in range(len(sample_data[key][i])):
                    sample_data[key][i][j] = sample_data[key][i][j].to(device)
        else:
            sample_data[key] = sample_data[key].to(device)

    net.eval()
    with torch.no_grad():
        source_end_points = net(sample_data)
        grasp_preds = pred_grasp_decode(source_end_points)  

    preds = grasp_preds[0].detach().cpu().numpy()
    gg = GraspGroup(preds)

    # print("====")
    # print(len(gg))
    # print("====")
    # if cfgs.collision_thresh >0:
    #     cloud = get_my_data(return_raw_cloud=True)  
    #     mfcdetector = ModelFreeCollisionDetector(cloud,voxel_size=cfgs.voxel_size_cd)
    #     collision_mask = mfcdetector.detect(gg, approach_dist=0.05, collision_thresh=cfgs.collision_thresh)
    #     gg = gg[~collision_mask]

    gg = gg.sort_by_score()


    # indices_to_remove = []
    # for i, grasp in enumerate(gg):
    #     if (grasp.translation[0] <= -0.2 or grasp.translation[0] >= 0.35) or \
    #         (grasp.translation[1] <= -0.2 or grasp.translation[1] >= 0.1):
    #         indices_to_remove.append(i)


    # for i, grasp in enumerate(gg):
    #     if grasp.score < 0.15:
    #         indices_to_remove.append(i)
    # gg = gg.remove(indices_to_remove)


    # print(gg[0].score)    

    grippers = gg.to_open3d_geometry_list()
    cloud = o3d.geometry.PointCloud()
    cloud.points = o3d.utility.Vector3dVector(raw_pointclouds.astype(np.float32))
    cloud.colors = o3d.utility.Vector3dVector(np.asarray(raw_color, dtype=np.float32))
    o3d.visualization.draw_geometries([cloud, *grippers[:100]])

   

if __name__ == '__main__':
        
    inference()

    # inference(chosen_model='mega.tar')

