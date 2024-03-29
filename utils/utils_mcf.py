import numpy as np
import networkx as nx
import queue
from collections import deque
from utils.utils_stitcher_cost import stitch_cost, stitch_cost_simple_distance
# from scipy import stats
from i24_logger.log_writer import catch_critical
import itertools
import _pickle as pickle

    
class Fragment():
    # Constructor to create a new fragment
    def __init__(self, traj_doc):
        '''
        just a simple object that keeps all the important information from a trajectory document for tracking
        - id, timestamp, x_position, y_position, length, width, last_timsetamp, first_timestamp, direction
        '''
        
        self.data = traj_doc
            
    def __repr__(self):
        try:
            return 'Fragment({!r})'.format(self.data["ID"])
        except:
            return 'Fragment({!r})'.format(self.data["_id"])
    

        


class MOTGraphSingle:
    '''
    same as MOT_Graph except that every fragment is represented as a single node. this is equivalent to say that the inclusion cost for each fragment is 0, or the false positive rate is 0
    a fragment must be included in the trajectories
    '''
    def __init__(self, direction=None, attr = "ID", parameters = None):
        # self.parameters = parameters
        self.G = nx.DiGraph()
        # self.G.add_nodes_from(["s","t"])
        # self.G.nodes["s"]["subpath"] = []
        self.G.add_node("t")
        self.G.nodes["t"]["subpath"] = []
        self.all_paths = []
        self.attr = attr
        self.in_graph_deque = deque() # keep track of fragments that are currently in graph, ordered by last_timestamp
                        
        self.TIME_WIN = parameters["time_win"]
        self.stitcher_mode = parameters["stitcher_mode"]
        self.param = parameters["stitcher_args"]
        if parameters["stitcher_mode"] == "master":
            self.param["time_win"] = parameters["master_time_win"]
            self.param["stitch_thresh"] = self.param["master_stitch_thresh"]
        else:
            self.param["time_win"] = parameters["time_win"]
        self.compute_node_pos_map = {key:val for val,key in enumerate(parameters["compute_node_list"])}   
        self.cache = {}
        self.direction = direction
          
    # @catch_critical(errors = (Exception))
    def add_node(self, fragment):
        '''
        add one node i in G [no inclusion edge]
        add edge t->i, mark the edge as match = True [exiting edge]
        update distance from t
        add all incident edges from i to other possible nodes, mark edges as match = False
        earlier fgmt are removed, information rolls towrads the end (in time) in graph path
        '''
        # new_id = getattr(fragment, self.attr)
        new_id = fragment[self.attr]
        self.G.add_edge("t", new_id, weight=0, match=True)
        self.G.nodes[new_id]["subpath"] = [new_id] # list of ids
        self.G.nodes[new_id]["last_timestamp"] = fragment["last_timestamp"]
        # self.G.nodes[new_id]["ending_x"] = fragment["ending_x"]
        # self.G.nodes[new_id]["filters"] = [fragment["filter"]] # list of lists
        self.cache[new_id] = fragment
            
        nc = len(self.in_graph_deque)
        node_id = fragment["compute_node_id"]
        
        if self.stitcher_mode == "local":
            node_diff_thresh = 0
        else:
            node_diff_thresh = 1
        for i in range(nc):
            fgmt = self.in_graph_deque[nc-1-i]
            # if timeout, no need to check for older fragments
            gap = fragment["timestamp"][0] - fgmt["timestamp"][-1] 
            if gap > self.param["time_win"]:
                break
                
            fgmt_node_id = fgmt["compute_node_id"]
            # print(str(fgmt["_id"])[-4:], fgmt_node_id)
            
            # stitch the same node_id in local mode
            # if self.stitcher_mode == "local" and fgmt_node_id == node_id:
            #     cost = stitch_cost(fgmt, fragment, self.TIME_WIN, self.param)

            if abs(self.compute_node_pos_map[node_id]-self.compute_node_pos_map[fgmt_node_id]) <= node_diff_thresh:
                cost = stitch_cost(fgmt, fragment, self.TIME_WIN, self.param)
#                 print(str(fgmt["_id"])[-4:], str(fragment["_id"])[-4:], cost)
            else:
                cost = 1e5
            
            if cost <= self.param["stitch_thresh"]:  # new edge points from new_id to existing nodes, with postive cost
                fgmt_id = fgmt[self.attr]
                self.G.add_edge(new_id, fgmt_id, weight = self.param["stitch_thresh"]-cost, match = False)
        
        # add Fragment pointer to cache
        self.in_graph_deque.append(fragment)

        # check for time-out fragments in deque and compress paths
        while self.in_graph_deque[0]["last_timestamp"] < fragment["first_timestamp"] - self.TIME_WIN:
            fgmt = self.in_graph_deque.popleft()
            fgmt_id = fgmt[self.attr]
            try:
                for v,_,data in self.G.in_edges(fgmt_id, data = True):
                    if data["match"] and v != "t":
                        # compress fgmt and v -> roll up subpath 
                        # TODO: need to check the order
                        self.G.nodes[v]["subpath"].extend(self.G.nodes[fgmt_id]["subpath"])
                        # self.G.nodes[v]["filters"].extend(self.G.nodes[fgmt_id]["filters"])
                        self.G.remove_node(fgmt_id)
                        break
            except nx.exception.NetworkXError:
                # if fgmt_id in self.G.nodes(): 
                #     print(f"{fgmt_id} is not cleaned during add_node")
                pass

        
    # @catch_critical(errors = (Exception))
    def verify_path(self, path, cost_thresh = 10):
        # double check if any pair in path have conflict (large cost)
        # path is ordered by last_timestamp
        
        comb = list(itertools.combinations(path, 2))
        for id1, id2 in comb:
            f1 = self.cache[id1]
            f2 = self.cache[id2]
            cost = stitch_cost(f1, f2, self.TIME_WIN, self.param)
            # print(cost)
            if cost > cost_thresh:
                return False # detect high cost
        return True
    
    
    # @catch_critical(errors = (Exception))       
    def clean_graph(self, path):
        '''
        remove all nodes in path from G and in_graph_deque
        in_graph_deque() is not cleaned
        it is cleaned during add_node iteration
        '''
        for node in path:
            try:
                self.cache.pop(node)
            except:
                pass
            try:
                self.G.remove_node(node)
            except: # if node is not in graph
                # print(f"exception during clean_graph, {e}")
                pass
        
    
    # @catch_critical(errors = (Exception))        
    def find_legal_neighbors(self, node):
        '''
        find ``neighbors`` of node x in G such that 
        cost(x, u) - cost(u,v) > 0, and (x,u) is unmatched, and (u,v) is matched i.e., positive delta if x steals u from v
        the idea is similar to alternating path in Hungarian algorithm
        '''
        nei = []
        for u in self.G.adj[node]:
            if not self.G[node][u]["match"]:
                cost_p = self.G[node][u]["weight"]
                # print(node, u, cost_p)
                for v,_ ,data in self.G.in_edges(u, data=True):
                    if data["match"]:
                        cost_m = self.G[v][u]["weight"]
                        if cost_p - cost_m > 0:
                            nei.append([u,v,cost_p - cost_m])
                        
        # print("legal nei for {} is {}".format(node, nei))
        return nei

            
            
    # @catch_critical(errors = (Exception))    
    def find_alternating_path(self, root):
        '''
        construct an alternative matching tree (Hungarian tree) from root, alternate between unmatched edge and matched edge
        terminate until a node cannot change the longest distance of its outgoing neighbors
        TODO: simultaneously build tree and keep track of the longest distance path from root to leaf, output that path
        '''
        q = queue.Queue()
        q.put((root, [root], 0)) # keep track of current node, path from root, cost delta of the path
        best_dist = -1
        explored = set()
        best_path = None
        steps = 0
        
        while not q.empty():
            x, path_to_x, dist_x = q.get()
            explored.add(x)
            nei = self.find_legal_neighbors(x)
            if not nei:
                if dist_x > best_dist:
                    best_dist = dist_x
                    best_path = path_to_x
            for u, v, delta in nei:
                if v == "t":
                    if dist_x + delta > best_dist:
                        best_dist = dist_x + delta
                        best_path = path_to_x + [u, v]
                if u not in explored:
                    q.put((v, path_to_x + [u, v], dist_x + delta))
            steps +=1
           
#         try: pl = len(best_path)
#         except: pl = 0
        # print("alt path starting at {} | path length:{} | bfs steps:{}".format(root, pl, steps))           
        return best_path, best_dist
    
    
    # @catch_critical(errors = (Exception))
    def augment_path(self, node):
        '''
        calculate an alternating path by adding node to G (assume node is already properly added to G)
        reverse that path in G (switch match bool)
        '''
                
        alt_path, cost = self.find_alternating_path(node)
        
        # TODO: why alt_path could be None? if node does not have any connections, alt-path should be [node]
        if alt_path is None: 
            print("** alt_path for {} is None. Write to pkl file".format(node))
            with open(f'none_alt_path_{node}.pkl', 'wb') as handle:
                pickle.dump(self, handle)
        else:
            forward = True
            for i in range(len(alt_path)-1):
                if forward:
                    self.G[alt_path[i]][alt_path[i+1]]["match"] = True
                else:
                    self.G[alt_path[i+1]][alt_path[i]]["match"] = False
                forward = not forward
        
    # @catch_critical(errors = (Exception))
    def get_next_match(self, node):
        for curr, next, data in self.G.out_edges(node, data=True):
            if data["match"]:
                # print(curr, next, data)
                return next
        return None  
    
    
    # @catch_critical(errors = (Exception))
    def get_all_traj(self):
        '''
        only called at final flushing
        traverse G along matched edges
        '''
        self.all_paths = [] # list of lists [[id1, id2],[id3, id4]]
        # self.all_filters = [] # list of lists of lists [[[1,1,0,0],[0,1,0]], [[0,0,1],[1,1]]]
        
        def dfs(node, path):
            if not node: # at the leaf
                self.all_paths.append(list(path))
                
                return list(path)
            path = path + self.G.nodes[node]["subpath"]
            next = self.get_next_match(node)
            # print("curr: {},next: {}".format(node, next))
            return dfs(next, path)
            
        tails =  self.G.adj["t"]
        for tail in tails:
            if self.G["t"][tail]["match"]:
                one_path = dfs(tail, [])
                # self.clean_graph([i for sublist in self.all_paths for i in sublist])
                
        return self.all_paths
            
        
    # @catch_critical(errors = (Exception))
    def pop_path(self, time_thresh):
        '''
        examine tail and pop if timeout (last_timestamp < time_thresh)
        remove the paths from G
        return paths
        '''
        all_paths = [] # list of lists [[id1, id2],[id3, id4]]
        # all_filters = [] # list of lists of lists [[[1,1,0,0],[0,1,0]], [[0,0,1],[1,1]]]
        
        def dfs(node, path):
            if not node: # at the leaf
                all_paths.append(list(path))
                
                return list(path)
            
            path = path + self.G.nodes[node]["subpath"]
            next = self.get_next_match(node)
            return dfs(next, path)
            
        tails =  self.G.adj["t"]
        
        for tail in tails:
            if tail in self.G.nodes and self.G["t"][tail]["match"] \
            and self.G.nodes[tail]["last_timestamp"] < time_thresh:
#                 if (self.direction == "eb" and self.G.nodes[tail]["ending_x"] <= dist_thresh) \
#                     or (self.direction == "wb" and self.G.nodes[tail]["ending_x"] > dist_thresh):
                one_path = dfs(tail, [])
                
                # print("*** tail: ", tail, one_path)
                # self.clean_graph(one_path)
                
        # 6/11/2023 remove isolated nodes in G TODO: not sure why they appear in the first place
        self.G.remove_nodes_from(list(nx.isolates(self.G)))
        
                
        return all_paths
        
    
    # @catch_critical(errors = (Exception))
    def get_filters(self, path):
        filters = []
        for _id in path:
            try:
                filters.extend(self.G.nodes[_id]["filters"])
            except KeyError:
                pass
        return filters
    
    
    # @catch_critical(errors = (Exception))
    def get_traj_dicts(self, path):
        '''
        get a list of corresponding traj dictionaries of path
        '''
        trajs = []
        for _id in path:
            trajs.append(self.cache[_id])
        return trajs
            
       
        
       
        
       
if __name__ == '__main__':
    print("not implemented")