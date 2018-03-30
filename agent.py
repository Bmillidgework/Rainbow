import os
import torch
from torch import nn, optim
from torch.autograd import Variable

from model import DQN


class Agent():
  def __init__(self, args, env):
    self.action_space = env.action_space()
    self.atoms = args.atoms
    self.Vmin = args.V_min
    self.Vmax = args.V_max

    self.delta_z = (args.V_max - args.V_min) / (args.atoms - 1)
    self.batch_size = args.batch_size
    self.n = args.multi_step
    self.discount = args.discount

    self.online_net = DQN(args, self.action_space)
    if args.model and os.path.isfile(args.model):
      self.online_net.load_state_dict(torch.load(args.model))
    self.online_net.train()

    self.target_net = DQN(args, self.action_space)
    self.update_target_net()
    self.target_net.train()
    for param in self.target_net.parameters():
      param.requires_grad = False

    self.optimiser = optim.Adam(self.online_net.parameters(), lr=args.lr, eps=args.adam_eps)
    if args.cuda:
      self.online_net.cuda()
      self.target_net.cuda()

  # Resets noisy weights in all linear layers (of online net only)
  def reset_noise(self):
    self.online_net.reset_noise()

  # Acts based on single state (no batch)
  def act(self, state):
    return self.online_net.act(state)

  def learn(self, mem):
    # Sample transitions
    idxs, states, actions, returns, next_states, nonterminals, weights = mem.sample(self.batch_size)

    # Calculate current state probabilities
    self.online_net.reset_noise()  # Sample new noise for online network
    ps = self.online_net(states)  # Probabilities p(s_t, ·; θonline)
    ps_a = ps[range(self.batch_size), actions]  # p(s_t, a_t; θonline)

    # Calculate nth next state probabilities
    self.online_net.reset_noise()  # Sample new noise for action selection
    pns = self.online_net(next_states).data  # Probabilities p(s_t+n, ·; θonline)
    dns = self.online_net.support.expand_as(pns) * pns  # Distribution d_t+n = (z, p(s_t+n, ·; θonline))
    argmax_indices_ns = dns.sum(2).max(1)[1]  # Perform argmax action selection using online network: argmax_a[(z, p(s_t+n, a; θonline))]
    self.target_net.reset_noise()  # Sample new target net noise
    pns = self.target_net(next_states).data  # Probabilities p(s_t+n, ·; θtarget)
    pns_a = pns[range(self.batch_size), argmax_indices_ns]  # Double-Q probabilities p(s_t+n, argmax_a[(z, p(s_t+n, a; θonline))]; θtarget)
    if nonterminals.min() == 0:
      terminals = (1 - nonterminals.squeeze()).nonzero().squeeze()
      pns_a[terminals] = 1 / self.atoms  # Divide probability equally

    # Compute Tz (Bellman operator T applied to z)
    Tz = returns.unsqueeze(1) + nonterminals * (self.discount ** self.n) * self.online_net.support.unsqueeze(0)  # Tz = R^n + (γ^n)z (accounting for terminal states)
    Tz = Tz.clamp(min=self.Vmin, max=self.Vmax)  # Clamp between supported values
    # Compute L2 projection of Tz onto fixed support z
    b = (Tz - self.Vmin) / self.delta_z  # b = (Tz - Vmin) / Δz
    l, u = b.floor().long(), b.ceil().long()

    # Distribute probability of Tz
    m = states.data.new(self.batch_size, self.atoms).zero_()
    offset = torch.linspace(0, ((self.batch_size - 1) * self.atoms), self.batch_size).long().unsqueeze(1).expand(self.batch_size, self.atoms).type_as(actions)
    m.view(-1).index_add_(0, (l + offset).view(-1), (pns_a * (u.float() - b)).view(-1))  # m_l = m_l + p(s_t+n, a*)(u - b)
    m.view(-1).index_add_(0, (u + offset).view(-1), (pns_a * (b - l.float())).view(-1))  # m_u = m_u + p(s_t+n, a*)(b - l)

    ps_a = ps_a.clamp(min=1e-3)  # Clamp for numerical stability in log
    loss = -torch.sum(Variable(m) * ps_a.log(), 1)  # Cross-entropy loss (minimises DKL(m||p(s_t, a_t)))
    self.online_net.zero_grad()
    (weights * loss).mean().backward()  # Importance weight losses
    self.optimiser.step()

    mem.update_priorities(idxs, loss.data)  # Update priorities of sampled transitions

  def update_target_net(self):
    self.target_net.load_state_dict(self.online_net.state_dict())

  def share_memory(self):
    self.online_net.share_memory()
