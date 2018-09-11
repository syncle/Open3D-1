# Open3D: www.open3d.org
# The MIT License (MIT)
# See license file or visit www.open3d.org for details

# examples/Python/Tutorial/ReconstructionSystem/register_fragments.py

import numpy as np
import sys
sys.path.append("../Utility")
from open3d import *
from common import *
from optimize_posegraph import *


def preprocess_point_cloud(pcd, config):
    voxel_size = config["voxel_size"]
    pcd_down = voxel_down_sample(pcd, voxel_size)
    estimate_normals(pcd_down,
            KDTreeSearchParamHybrid(radius = voxel_size * 2.0, max_nn = 30))
    pcd_fpfh = compute_fpfh_feature(pcd_down,
            KDTreeSearchParamHybrid(radius = voxel_size * 5.0, max_nn = 100))
    return (pcd_down, pcd_fpfh)


def register_point_cloud_fpfh(source, target,
        source_fpfh, target_fpfh, config):
    distance_threshold = config["voxel_size"] * 1.5
    if config["global_registration"] == "fgr":
        result = registration_fast_based_on_feature_matching(
                source, target, source_fpfh, target_fpfh,
                FastGlobalRegistrationOption(
                maximum_correspondence_distance = distance_threshold))
    if config["global_registration"] == "ransac":
        result = registration_ransac_based_on_feature_matching(
                source, target, source_fpfh, target_fpfh,
                distance_threshold,
                TransformationEstimationPointToPoint(False), 4,
                [CorrespondenceCheckerBasedOnEdgeLength(0.9),
                CorrespondenceCheckerBasedOnDistance(distance_threshold)],
                RANSACConvergenceCriteria(4000000, 500))
    if (result.transformation.trace() == 4.0):
        return (False, np.identity(4))
    else:
        return (True, result)


def compute_initial_registration(s, t, source_down, target_down,
        source_fpfh, target_fpfh, path_dataset, config):

    if t == s + 1: # odometry case
        print("Using RGBD odometry")
        pose_graph_frag = read_pose_graph(path_dataset +
                template_fragment_posegraph_optimized % s)
        n_nodes = len(pose_graph_frag.nodes)
        transformation = np.linalg.inv(
                pose_graph_frag.nodes[n_nodes-1].pose)
        print(transformation)
    else: # loop closure case
        print("register_point_cloud_fpfh")
        (success_ransac, result_ransac) = register_point_cloud_fpfh(
                source_down, target_down,
                source_fpfh, target_fpfh, config)
        if not success_ransac:
            print("No resonable solution. Skip this pair")
            return (False, np.identity(4))
        else:
            transformation = result_ransac.transformation
        print(transformation)

    if config["debug_mode"]:
        draw_registration_result(source_down, target_down,
                transformation)
    return (True, transformation)


def multiscale_icp(source, target, voxel_size, max_iter,
        config, init_transformation = np.identity(4)):
    current_transformation = init_transformation
    for scale in range(len(max_iter)): # multi-scale approach
        iter = max_iter[scale]
        print("voxel_size %f" % voxel_size[scale])
        source_down = voxel_down_sample(source, voxel_size[scale])
        target_down = voxel_down_sample(target, voxel_size[scale])
        estimate_normals(source_down, KDTreeSearchParamHybrid(
                radius = voxel_size[scale] * 2.0, max_nn = 30))
        estimate_normals(target_down, KDTreeSearchParamHybrid(
                radius = voxel_size[scale] * 2.0, max_nn = 30))
        if config["icp_method"] == "point_to_point":
            result_icp = registration_icp(source_down, target_down,
                    voxel_size[scale] * 1.4, current_transformation,
                    TransformationEstimationPointToPlane(),
                    ICPConvergenceCriteria(max_iteration = iter))
        else:
            # colored pointcloud registration
            # This is implementation of following paper
            # J. Park, Q.-Y. Zhou, V. Koltun,
            # Colored Point Cloud Registration Revisited, ICCV 2017
            result_icp = registration_colored_icp(source_down, target_down,
                    voxel_size[scale], current_transformation,
                    ICPConvergenceCriteria(relative_fitness = 1e-6,
                    relative_rmse = 1e-6, max_iteration = iter))
        current_transformation = result_icp.transformation

    maximum_correspondence_distance = config["voxel_size"] * 1.4
    information_matrix = get_information_matrix_from_point_clouds(
            source, target, maximum_correspondence_distance,
            result_icp.transformation)
    if config["debug_mode"]:
        draw_registration_result_original_color(source, target,
                result_icp.transformation)
    return (result_icp.transformation, information_matrix)


def local_refinement(s, t, source, target, transformation_init, config):
    voxel_size = config["voxel_size"]
    if t == s + 1: # odometry case
        print("register_point_cloud_icp")
        (transformation, information) = \
                multiscale_icp(
                source, target, [voxel_size / 4.0], [30],
                config, transformation_init)
    else: # loop closure case
        print("register_colored_point_cloud")
        (transformation, information) = \
                multiscale_icp(
                source, target,
                [voxel_size, voxel_size/2.0, voxel_size/4.0], [50, 30, 14],
                config, transformation_init)

    success_local = False
    if information[5,5] / min(len(source.points),len(target.points)) > 0.3:
        success_local = True
    if config["debug_mode"]:
        draw_registration_result_original_color(
                source, target, transformation)
    return (success_local, transformation, information)


def update_posegrph_for_scene(s, t, transformation, information,
        odometry, pose_graph):
    if t == s + 1: # odometry case
        odometry = np.dot(transformation, odometry)
        odometry_inv = np.linalg.inv(odometry)
        pose_graph.nodes.append(PoseGraphNode(odometry_inv))
        pose_graph.edges.append(
                PoseGraphEdge(s, t, transformation,
                information, uncertain = False))
    else: # loop closure case
        pose_graph.edges.append(
                PoseGraphEdge(s, t, transformation,
                information, uncertain = True))
    return (odometry, pose_graph)


def register_point_cloud_pair(ply_file_names, s, t, config):
    print("reading %s ..." % ply_file_names[s])
    source = read_point_cloud(ply_file_names[s])
    print("reading %s ..." % ply_file_names[t])
    target = read_point_cloud(ply_file_names[t])
    (source_down, source_fpfh) = preprocess_point_cloud(source, config)
    (target_down, target_fpfh) = preprocess_point_cloud(target, config)
    (success_global, transformation_init) = \
            compute_initial_registration(
            s, t, source_down, target_down,
            source_fpfh, target_fpfh, config["path_dataset"], config)
    if t != s + 1 and not success_global:
        return (False, np.identity(4), np.identity(6))
    (success_local, transformation_icp, information_icp) = \
            local_refinement(s, t, source, target,
            transformation_init, config)
    if t != s + 1 and not success_local:
        return (False, np.identity(4), np.identity(6))
    if config["debug_mode"]:
        print(transformation_icp)
        print(information_icp)
    return (True, transformation_icp, information_icp)


# other types instead of class?
class matching_result:
    def __init__(self, s, t):
        self.s = s
        self.t = t
        self.success = False
        self.transformation = np.identity(4)
        self.infomation = np.identity(6)


def make_posegraph_for_scene(ply_file_names, config):
    pose_graph = PoseGraph()
    odometry = np.identity(4)
    pose_graph.nodes.append(PoseGraphNode(odometry))
    info = np.identity(6)

    n_files = len(ply_file_names)
    matching_results = {}
    for s in range(n_files):
        for t in range(s + 1, n_files):
            matching_results[s * n_files + t] = matching_result(s, t)

    if config["python_multi_threading"]:
        from joblib import Parallel, delayed
        import multiprocessing
        import subprocess
        MAX_THREAD = 144 # configured for vcl-cpu cluster
        cmd = 'export OMP_PROC_BIND=true ; export GOMP_CPU_AFFINITY="0-%d"' % MAX_THREAD # have effect
        p = subprocess.call(cmd, shell=True)
        num_cores = multiprocessing.cpu_count()
        results = Parallel(n_jobs=MAX_THREAD)(
                delayed(register_point_cloud_pair)(ply_file_names,
                matching_results[r].s, matching_results[r].t, config)
                for r in matching_results)
        print(results)
        for i, r in enumerate(matching_results):
            matching_results[r].success = results[i][0]
            matching_results[r].transformation = results[i][1]
            matching_results[r].information = results[i][2]
    else:
        for r in matching_results:
            (matching_results[r].success, matching_results[r].transformation,
                    matching_results[r].information) = \
                    register_point_cloud_pair(ply_file_names,
                    matching_results[r].s, matching_results[r].t, config)

    for r in matching_results:
        if matching_results[r].success:
            (odometry, pose_graph) = update_posegrph_for_scene(
                    matching_results[r].s, matching_results[r].t,
                    matching_results[r].transformation,
                    matching_results[r].information,
                    odometry, pose_graph)
            print(pose_graph)
    write_pose_graph(os.path.join(config["path_dataset"],
            template_global_posegraph), pose_graph)


def run(config):
    print("register fragments.")
    set_verbosity_level(VerbosityLevel.Debug)
    ply_file_names = get_file_list(os.path.join(
            config["path_dataset"], folder_fragment), ".ply")
    make_folder(os.path.join(config["path_dataset"], folder_scene))
    make_posegraph_for_scene(ply_file_names, config)
    optimize_posegraph_for_scene(config["path_dataset"], config)