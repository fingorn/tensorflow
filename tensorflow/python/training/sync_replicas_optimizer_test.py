# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Tests for sync_replicas_optimizer.py."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time

import portpicker
import tensorflow as tf


def create_local_cluster(num_workers, num_ps, protocol="grpc"):
  """Create local GRPC servers and return them."""
  worker_ports = [portpicker.pick_unused_port() for _ in range(num_workers)]
  ps_ports = [portpicker.pick_unused_port() for _ in range(num_ps)]
  cluster_dict = {
      "worker": ["localhost:%s" % port for port in worker_ports],
      "ps": ["localhost:%s" % port for port in ps_ports]}
  cs = tf.train.ClusterSpec(cluster_dict)

  workers = [
      tf.train.Server(
          cs, job_name="worker", protocol=protocol, task_index=ix, start=True)
      for ix in range(num_workers)]
  ps_servers = [
      tf.train.Server(
          cs, job_name="ps", protocol=protocol, task_index=ix, start=True)
      for ix in range(num_ps)]

  return workers, ps_servers


# Creates the workers and return their sessions, graphs, train_ops.
def get_workers(num_workers, replicas_to_aggregate, workers):
  sessions = []
  graphs = []
  train_ops = []
  for worker_id in range(num_workers):
    graph = tf.Graph()
    is_chief = (worker_id == 0)
    with graph.as_default():
      with tf.device("/job:ps/task:0"):
        global_step = tf.Variable(0, name="global_step", trainable=False)
        var_0 = tf.Variable(0.0, name="v0")
      with tf.device("/job:ps/task:1"):
        var_1 = tf.Variable(1.0, name="v1")
        var_sparse = tf.Variable([[3.0], [4.0]], name="v_sparse")

      with tf.device("/job:worker/task:"+str(worker_id)):
        grads_0 = tf.constant(0.1+worker_id*0.2)
        grads_1 = tf.constant(0.9+worker_id*0.2)
        # This is to test against sparse gradients.
        grads_sparse = tf.IndexedSlices(
            tf.constant([0.1+worker_id*0.2], shape=[1, 1]),
            tf.constant([1]),
            tf.constant([2, 1]))
        sgd_opt = tf.train.GradientDescentOptimizer(2.0)
        sync_rep_opt = tf.train.SyncReplicasOptimizerV2(
            sgd_opt, replicas_to_aggregate=replicas_to_aggregate,
            total_num_replicas=num_workers)
        train_op = [sync_rep_opt.apply_gradients(
            zip([grads_0, grads_1, grads_sparse], [var_0, var_1, var_sparse]),
            global_step=global_step)]

        init_op = tf.global_variables_initializer()
        # Needed ops from the sync_rep optimizer. This is mainly for the
        # local_step initialization.
        local_init_op = sync_rep_opt.local_step_init_op
        if is_chief:
          local_init_op = sync_rep_opt.chief_init_op
        ready_for_local_init_op = sync_rep_opt.ready_for_local_init_op

        # Chief_queue_runner
        chief_queue_runner = sync_rep_opt.get_chief_queue_runner()
        sync_init_op = sync_rep_opt.get_init_tokens_op(num_workers)

    # Creates session for chief.
    supervisor = tf.train.Supervisor(
        graph=graph,
        is_chief=is_chief,
        recovery_wait_secs=1,
        init_op=init_op,
        local_init_op=local_init_op,
        ready_for_local_init_op=ready_for_local_init_op)
    session = supervisor.prepare_or_wait_for_session(workers[worker_id].target)

    # Chief should execute the sync_init_op and start the chief queue runner.
    if is_chief:
      session.run(sync_init_op)
      supervisor.StartQueueRunners(session, [chief_queue_runner])

    sessions.append(session)
    graphs.append(graph)
    train_ops.append(train_op)

  return sessions, graphs, train_ops


class SyncReplicasOptimizerV2Test(tf.test.TestCase):

  def _run(self, train_op, sess):
    sess.run(train_op)

  def test2Workers(self):
    num_workers = 2
    replicas_to_aggregate = 2
    num_ps = 2
    workers, _ = create_local_cluster(num_workers=num_workers, num_ps=num_ps)

    # Creates and returns all the workers.
    sessions, graphs, train_ops = get_workers(num_workers,
                                              replicas_to_aggregate,
                                              workers)

    # Chief should have already initialized all the variables.
    var_0_g_0 = graphs[0].get_tensor_by_name("v0:0")
    var_1_g_0 = graphs[0].get_tensor_by_name("v1:0")
    local_step_0 = graphs[0].get_tensor_by_name("sync_rep_local_step:0")
    self.assertAllEqual(0.0, var_0_g_0.eval(session=sessions[0]))
    self.assertAllEqual(1.0, var_1_g_0.eval(session=sessions[0]))
    self.assertAllEqual(0, local_step_0.eval(session=sessions[0]))

    # Will just use session 1 to verify all the variables later.
    var_0_g_1 = graphs[1].get_tensor_by_name("v0:0")
    var_1_g_1 = graphs[1].get_tensor_by_name("v1:0")
    var_sparse_g_1 = graphs[1].get_tensor_by_name("v_sparse:0")
    local_step_1 = graphs[1].get_tensor_by_name("sync_rep_local_step:0")
    global_step = graphs[1].get_tensor_by_name("global_step:0")

    # The steps should also be initialized.
    self.assertAllEqual(0, global_step.eval(session=sessions[1]))
    self.assertAllEqual(0, local_step_1.eval(session=sessions[1]))
    self.assertAllClose([[3.0], [4.0]],
                        var_sparse_g_1.eval(session=sessions[1]))

    # We have initial tokens in the queue so we can call this one by one. After
    # the first step, this will no longer work as there will be no more extra
    # tokens in the queue.
    sessions[0].run(train_ops[0])
    sessions[1].run(train_ops[1])

    # The global step should have been updated and the variables should now have
    # the new values after the average of the gradients are applied.
    while global_step.eval(session=sessions[1]) != 1:
      time.sleep(0.01)

    self.assertAllClose(0-(0.1+0.3)/2*2.0, var_0_g_1.eval(session=sessions[1]))
    self.assertAllClose(1-(0.9+1.1)/2*2.0, var_1_g_1.eval(session=sessions[1]))
    self.assertAllClose([[3.0], [4.0-(0.1+0.3)/2*2.0]],
                        var_sparse_g_1.eval(session=sessions[1]))

    # The local step for both workers should still be 0 because the initial
    # tokens in the token queue are 0s. This means that the following
    # computation of the gradients will be wasted as local_step is smaller than
    # the current global step. However, this only happens once when the system
    # just starts and this is necessary to make the system robust for the case
    # when chief gets restarted by errors/preemption/...
    self.assertAllEqual(0, local_step_0.eval(session=sessions[0]))
    self.assertAllEqual(0, local_step_1.eval(session=sessions[1]))

    sessions[0].run(train_ops[0])
    sessions[1].run(train_ops[1])
    # Although the global step should still be 1 as explained above, the local
    # step should now be updated to 1. The variables are still the same.
    self.assertAllEqual(1, global_step.eval(session=sessions[1]))
    self.assertAllEqual(1, local_step_0.eval(session=sessions[0]))
    self.assertAllEqual(1, local_step_1.eval(session=sessions[1]))
    self.assertAllClose(0-(0.1+0.3)/2*2.0, var_0_g_1.eval(session=sessions[1]))
    self.assertAllClose(1-(0.9+1.1)/2*2.0, var_1_g_1.eval(session=sessions[1]))

    # At this step, the token queue is empty. So the 2 workers need to work
    # together to proceed.
    threads = []
    threads.append(self.checkedThread(target=self._run,
                                      args=(train_ops[0], sessions[0])))
    threads.append(self.checkedThread(target=self._run,
                                      args=(train_ops[1], sessions[1])))

    # The two workers starts to execute the train op.
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()

    # The global step should now be 2 and the gradients should have been
    # applied twice.
    self.assertAllEqual(2, global_step.eval(session=sessions[1]))
    self.assertAllClose(0 - 2 * (0.1 + 0.3) / 2 * 2.0,
                        var_0_g_1.eval(session=sessions[1]))
    self.assertAllClose(1 - 2 * (0.9 + 1.1) / 2 * 2.0,
                        var_1_g_1.eval(session=sessions[1]))

  # 3 workers and one of them is backup.
  def test3Workers1Backup(self):
    num_workers = 3
    replicas_to_aggregate = 2
    num_ps = 2
    workers, _ = create_local_cluster(num_workers=num_workers, num_ps=num_ps)

    # Creates and returns all the workers.
    sessions, graphs, train_ops = get_workers(num_workers,
                                              replicas_to_aggregate,
                                              workers)

    # Chief should have already initialized all the variables.
    var_0_g_1 = graphs[1].get_tensor_by_name("v0:0")
    var_1_g_1 = graphs[1].get_tensor_by_name("v1:0")
    local_step_1 = graphs[1].get_tensor_by_name("sync_rep_local_step:0")
    global_step = graphs[1].get_tensor_by_name("global_step:0")

    # The steps should also be initilized.
    self.assertAllEqual(0, global_step.eval(session=sessions[1]))
    self.assertAllEqual(0, local_step_1.eval(session=sessions[1]))

    # We have initial tokens in the queue so we can call this one by one. After
    # the token queue becomes empty, they should be called concurrently.
    # Here worker 0 and worker 2 finished first.
    sessions[0].run(train_ops[0])
    sessions[2].run(train_ops[2])

    # The global step should have been updated since we only need to collect 2
    # gradients. The variables should now have the new values after the average
    # of the gradients from worker 0/2 are applied.
    while global_step.eval(session=sessions[1]) != 1:
      time.sleep(0.01)

    self.assertAllEqual(1, global_step.eval(session=sessions[1]))
    self.assertAllClose(0-(0.1+0.5)/2*2.0, var_0_g_1.eval(session=sessions[1]))
    self.assertAllClose(1-(0.9+1.3)/2*2.0, var_1_g_1.eval(session=sessions[1]))

    # Worker 1 finished later and its gradients will now be dropped as it is
    # stale.
    sessions[1].run(train_ops[1])

    # As shown in the previous test, the local_step for all workers should be
    # still 0 so their next computation will also be dropped.
    sessions[0].run(train_ops[0])
    sessions[1].run(train_ops[1])
    sessions[2].run(train_ops[2])

    # Although the global step should still be 1 as explained above, the local
    # step should now be updated to 1. Just check worker 1 as an example.
    self.assertAllEqual(1, global_step.eval(session=sessions[1]))
    self.assertAllEqual(1, local_step_1.eval(session=sessions[1]))

    thread_0 = self.checkedThread(target=self._run,
                                  args=(train_ops[0], sessions[0]))
    thread_1 = self.checkedThread(target=self._run,
                                  args=(train_ops[1], sessions[1]))

    # Lets worker 0 execute first.
    # It will wait as we need 2 workers to finish this step and the global step
    # should be still 1.
    thread_0.start()
    self.assertAllEqual(1, global_step.eval(session=sessions[1]))

    # Starts worker 1.
    thread_1.start()
    thread_1.join()

    # The global step should now be 2 and the gradients should have been
    # applied again.
    self.assertAllEqual(2, global_step.eval(session=sessions[1]))
    self.assertAllClose(-0.6 -(0.1 + 0.3) / 2 * 2.0,
                        var_0_g_1.eval(session=sessions[1]))
    self.assertAllClose(-1.2 - (0.9 + 1.1) / 2 * 2.0,
                        var_1_g_1.eval(session=sessions[1]))

if __name__ == "__main__":
  tf.test.main()
