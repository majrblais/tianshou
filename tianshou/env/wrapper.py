import numpy as np
from collections import deque
from multiprocessing import Process, Pipe
try:
    import ray
except ImportError:
    pass

from tianshou.utils import CloudpickleWrapper


class EnvWrapper(object):
    def __init__(self, env):
        self.env = env

    def step(self, action):
        return self.env.step(action)

    def reset(self):
        self.env.reset()

    def seed(self, seed=None):
        if hasattr(self.env, 'seed'):
            self.env.seed(seed)

    def render(self):
        if hasattr(self.env, 'render'):
            self.env.render()

    def close(self):
        self.env.close()


class FrameStack(EnvWrapper):
    def __init__(self, env, stack_num):
        """Stack last k frames."""
        super().__init__(env)
        self.stack_num = stack_num
        self._frames = deque([], maxlen=stack_num)

    def step(self, action):
        obs, reward, done, info = self.env.step(action)
        self._frames.append(obs)
        return self._get_obs(), reward, done, info

    def reset(self):
        obs = self.env.reset()
        for _ in range(self.stack_num):
            self._frames.append(obs)
        return self._get_obs()

    def _get_obs(self):
        try:
            return np.concatenate(self._frames, axis=-1)
        except ValueError:
            return np.stack(self._frames, axis=-1)


class VectorEnv(object):
    """docstring for VectorEnv"""
    def __init__(self, env_fns, **kwargs):
        super().__init__()
        self.envs = [_() for _ in env_fns]
        self._reset_after_done = kwargs.get('reset_after_done', False)

    def __len__(self):
        return len(self.envs)

    def reset(self):
        return np.stack([e.reset() for e in self.envs])

    def step(self, action):
        result = zip(*[e.step(action[i]) for i, e in enumerate(self.envs)])
        obs, rew, done, info = zip(*result)
        if self._reset_after_done and sum(done):
            for i, e in enumerate(self.envs):
                if done[i]:
                    e.reset()
        return np.stack(obs), np.stack(rew), np.stack(done), np.stack(info)

    def seed(self, seed=None):
        for e in self.envs:
            if hasattr(e, 'seed'):
                e.seed(seed)

    def render(self):
        for e in self.envs:
            if hasattr(e, 'render'):
                e.render()

    def close(self):
        for e in self.envs:
            e.close()


class SubprocVectorEnv(object):
    """docstring for SubProcVectorEnv"""
    def __init__(self, env_fns, **kwargs):

        super().__init__()
        self.env_num = len(env_fns)
        self.closed = False
        self.parent_remote, self.child_remote = zip(*[Pipe() for _ in range(self.env_num)])
        self.processes = [
            Process(target=self.worker, args=(parent, child, CloudpickleWrapper(env_fn), kwargs), daemon=True)
            for (parent, child, env_fn) in zip(self.parent_remote, self.child_remote, env_fns)
        ]
        for p in self.processes:
            p.start()
        for c in self.child_remote:
            c.close()

    def __len__(self):
        return self.env_num

    def worker(self, parent, p, env_fn_wrapper, **kwargs):
        reset_after_done = kwargs.get('reset_after_done', True)
        parent.close()
        env = env_fn_wrapper.data()
        while True:
            cmd, data = p.recv()
            if cmd == 'step':
                obs, rew, done, info = env.step(data)
                if reset_after_done and done:
                    # s_ is useless when episode finishes
                    obs = env.reset()
                p.send([obs, rew, done, info])
            elif cmd == 'reset':
                p.send(env.reset())
            elif cmd == 'close':
                p.close()
                break
            elif cmd == 'render':
                p.send(env.render())
            elif cmd == 'seed':
                p.send(env.seed(data))
            else:
                raise NotImplementedError

    def step(self, action):
        for p, a in zip(self.parent_remote, action):
            p.send(['step', a])
        result = [p.recv() for p in self.parent_remote]
        obs, rew, done, info = zip(*result)
        return np.stack(obs), np.stack(rew), np.stack(done), np.stack(info)

    def reset(self):
        for p in self.parent_remote:
            p.send(['reset', None])
        return np.stack([p.recv() for p in self.parent_remote])

    def seed(self, seed):
        if np.isscalar(seed):
            seed = [seed for _ in range(self.env_num)]
        for p, s in zip(self.parent_remote, seed):
            p.send(['seed', s])
        for p in self.parent_remote:
            p.recv()

    def render(self):
        for p in self.parent_remote:
            p.send(['render', None])
        for p in self.parent_remote:
            p.recv()

    def close(self):
        if self.closed:
            return
        for p in self.parent_remote:
            p.send(['close', None])
        self.closed = True
        for p in self.processes:
            p.join()


class RayVectorEnv(object):
    """docstring for RayVectorEnv"""
    def __init__(self, env_fns, **kwargs):
        super().__init__()
        self.env_num = len(env_fns)
        self._reset_after_done = kwargs.get('reset_after_done', False)
        try:
            if not ray.is_initialized():
                ray.init()
        except NameError:
            raise ImportError('Please install ray to support VectorEnv: pip3 install ray -U')
        self.envs = [ray.remote(EnvWrapper).options(num_cpus=0).remote(e()) for e in env_fns]

    def __len__(self):
        return self.env_num

    def step(self, action):
        result_obj = [e.step.remote(action[i]) for i, e in enumerate(self.envs)]
        obs, rew, done, info = zip(*[ray.get(r) for r in result_obj])
        return np.stack(obs), np.stack(rew), np.stack(done), np.stack(info)

    def reset(self):
        result_obj = [e.reset.remote() for e in self.envs]
        return np.stack([ray.get(r) for r in result_obj])

    def seed(self, seed):
        if np.isscalar(seed):
            seed = [seed for _ in range(self.env_num)]
        result_obj = [e.seed.remote(s) for e, s in zip(self.envs, seed)]
        for r in result_obj:
            ray.get(r)

    def render(self):
        result_obj = [e.render.remote() for e in self.envs]
        for r in result_obj:
            ray.get(r)

    def close(self):
        result_obj = [e.close.remote() for e in self.envs]
        for r in result_obj:
            ray.get(r)
