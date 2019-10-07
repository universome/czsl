from typing import Tuple

import torch
import torch.nn.functional as F
from torch import Tensor

from src.utils.weights_importance import compute_diagonal_fisher
from src.utils.lll import prune_logits
from src.trainers.task_trainer import TaskTrainer


class EWCTaskTrainer(TaskTrainer):
    def _after_init_hook(self):
        if self.task_idx > 0:
            prev_trainer = self.main_trainer.task_trainers[self.task_idx - 1]
            self.fisher = compute_diagonal_fisher(self.model, self.train_dataloader, prev_trainer.output_mask)
            self.weights_prev = torch.cat([p.data.view(-1) for p in self.model.parameters()])

    def train_on_batch(self, batch:Tuple[Tensor, Tensor]):
        self.model.train()

        x = torch.tensor(batch[0]).to(self.device_name)
        y = torch.tensor(batch[1]).to(self.device_name)

        logits = self.model(x)
        pruned_logits = prune_logits(logits, self.output_mask)
        loss = self.criterion(pruned_logits, y)

        if self.task_idx > 0:
            reg = self.compute_regularization()
            loss += self.config.hp.synaptic_strength * reg

        self.optim.zero_grad()
        loss.backward()
        self.optim.step()

    def compute_regularization(self) -> Tensor:
        head_size = self.model.get_head_size()
        keep_prob = self.config.hp.get('fisher_keep_prob', 1.)
        weights_curr = torch.cat([p.view(-1) for p in self.model.parameters()])

        if keep_prob < 1:
            # Do not apply dropout to the classification head (TODO: why?)
            body_fisher = F.dropout(self.fisher[:-head_size], keep_prob)
            head_fisher = self.fisher[-head_size:]
            fisher = torch.cat([body_fisher, head_fisher])
        else:
            fisher = self.fisher

        reg = torch.dot((weights_curr - self.weights_prev).pow(2), fisher)

        return reg
