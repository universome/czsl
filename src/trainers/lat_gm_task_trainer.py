import random
from typing import Tuple, Iterable
from copy import deepcopy

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from torch.nn.utils.clip_grad import clip_grad_norm_
from tqdm import tqdm
from firelab.config import Config

from src.utils.losses import compute_gradient_penalty
from src.utils.lll import prune_logits
from src.models.lat_gm import LatGM
from src.trainers.task_trainer import TaskTrainer
from src.utils.weights_importance import compute_mse_grad, compute_diagonal_fisher
from src.utils.model_utils import get_number_of_parameters
from src.utils.data_utils import construct_output_mask, flatten
from src.utils.training_utils import construct_optimizer


class LatGMTaskTrainer(TaskTrainer):
    BaseModel = LatGM

    def _after_init_hook(self):
        prev_trainer = self.get_previous_trainer()

        attrs = self.model.attrs if hasattr(self.model, 'attrs') else None
        self.prev_model = self.BaseModel(self.config.hp.model, attrs).to(self.device_name)
        self.prev_model.load_state_dict(deepcopy(self.model.state_dict()))

        if self.config.hp.get('reset_head_before_each_task'):
            self.model.reset_discriminator()

        self.learned_classes = np.unique(flatten(self.main_trainer.class_splits[:self.task_idx])).tolist()
        self.learned_classes_mask = construct_output_mask(self.learned_classes, self.config.lll_setup.num_classes)
        self.seen_classes = np.unique(flatten(self.main_trainer.class_splits[:self.task_idx + 1])).tolist()
        self.seen_classes_mask = construct_output_mask(self.seen_classes, self.config.lll_setup.num_classes)
        self.init_writer()

        if not prev_trainer is None:
            self.weights_prev = torch.cat([p.data.view(-1) for p in self.model.embedder.parameters()])

            if self.config.hp.reg_strategy == 'mas':
                self.mse_grad = compute_mse_grad(self.model, self.train_dataloader, prev_trainer.output_mask)
            elif self.config.hp.reg_strategy == 'ewc':
                self.fisher = compute_diagonal_fisher(self.model, self.train_dataloader, prev_trainer.output_mask)
            else:
                raise NotImplementedError(f'Unknown regularization strategy: {self.config.hp.reg_strategy}')

    def construct_optimizer(self):
        return {
            'generator': construct_optimizer(self.get_parameters('generator'), self.config.hp.gen_optim),
            'discriminator': construct_optimizer(self.get_parameters('discriminator'), self.config.hp.discr_optim),
            'classifier': construct_optimizer(self.get_parameters('classifier'), self.config.hp.cls_optim),
            'embedder': construct_optimizer(self.get_parameters('embedder'), self.config.hp.embedder_optim),
        }

    def get_parameters(self, name: str) -> Iterable[nn.Parameter]:
        params_dict = {
            'generator': self.model.generator.parameters(),
            'discriminator': self.model.discriminator.get_adv_parameters(),
            'classifier': self.model.discriminator.get_cls_parameters(),
            'embedder': self.model.embedder.parameters(),
        }

        return params_dict[name]

    def train_on_batch(self, batch: Tuple[np.ndarray, np.ndarray]):
        self.model.train()

        x = torch.tensor(batch[0]).to(self.device_name)
        y = torch.tensor(batch[1]).to(self.device_name)

        if self.num_epochs_done < self.config.hp.num_gan_epochs:
            self.generator_step(y)
            self.discriminator_step(x, y)
        else:
            self.classifier_step(x, y)

        # self.creativity_step()

    def discriminator_step(self, imgs: Tensor, y: Tensor):
        with torch.no_grad():
            x = self.model.embedder(imgs)
            z = self.model.generator.sample_noise(imgs.size(0)).to(self.device_name)
            x_fake = self.model.generator(z, y) # TODO: try picking y randomly

        adv_logits_on_real = self.model.discriminator.run_adv_head(x)
        adv_logits_on_fake = self.model.discriminator.run_adv_head(x_fake)

        adv_loss = -adv_logits_on_real.mean() + adv_logits_on_fake.mean()
        grad_penalty = compute_gradient_penalty(self.model.discriminator.run_adv_head, x, x_fake)

        total_loss = adv_loss + self.config.hp.loss_coefs.gp * grad_penalty

        self.perform_optim_step(total_loss, 'discriminator')

        self.writer.add_scalar('discr/adv_loss', adv_loss.item(), self.num_iters_done)
        self.writer.add_scalar('discr/mean_pred_real', adv_logits_on_real.mean().item(), self.num_iters_done)
        self.writer.add_scalar('discr/mean_pred_fake', adv_logits_on_fake.mean().item(), self.num_iters_done)
        self.writer.add_scalar('discr/grad_penalty', grad_penalty.item(), self.num_iters_done)

    def generator_step(self, y: Tensor):
        if self.num_iters_done % self.config.hp.num_discr_steps_per_gen_step != 0: return

        z = self.model.generator.sample_noise(y.size(0)).to(self.device_name)
        x_fake = self.model.generator(z, y)
        adv_logits_on_fake, cls_logits_on_fake = self.model.discriminator(x_fake)
        pruned_logits_on_fake = prune_logits(cls_logits_on_fake, self.output_mask)

        adv_loss = -adv_logits_on_fake.mean()
        distillation_loss = self.gen_knowledge_distillation_loss()
        cls_loss = F.cross_entropy(pruned_logits_on_fake, y)
        cls_acc = (pruned_logits_on_fake.argmax(dim=1) == y).float().mean().detach().cpu()

        total_loss = adv_loss \
            + self.config.hp.loss_coefs.distill * distillation_loss \
            + self.config.hp.loss_coefs.gen_cls * cls_loss

        self.perform_optim_step(total_loss, 'generator')

        self.writer.add_scalar(f'gen/adv_loss', adv_loss.item(), self.num_iters_done)
        self.writer.add_scalar(f'gen/distillation_loss', distillation_loss.item(), self.num_iters_done)
        self.writer.add_scalar(f'gen/cls_loss', cls_loss.item(), self.num_iters_done)
        self.writer.add_scalar(f'gen/cls_acc', cls_acc.item(), self.num_iters_done)

    def gen_knowledge_distillation_loss(self) -> Tensor:
        if len(self.learned_classes) == 0: return torch.tensor(0.)

        num_samples_per_class = self.config.hp.distill_batch_size // len(self.learned_classes)
        assert num_samples_per_class >= 0, "Distillation batch size is too small to capture all the classes."
        y = np.tile(np.array(self.learned_classes), num_samples_per_class)
        y = torch.tensor(y).to(self.device_name)
        z = self.model.generator.sample_noise(len(y)).to(self.device_name)
        outputs_teacher = self.prev_model.generator(z, y).view(len(y), -1)
        outputs_student = self.model.generator(z, y).view(len(y), -1)
        loss = torch.norm(outputs_teacher - outputs_student, dim=1).pow(2).mean()

        return loss / len(self.learned_classes)

    def classifier_step(self, x: Tensor, y: Tensor):
        pruned = self.model.compute_pruned_predictions(x, self.output_mask)
        curr_loss = self.criterion(pruned, y)
        curr_acc = (pruned.argmax(dim=1) == y).float().mean().detach().cpu()

        # if self.task_idx > 0:
        #     reg = self.compute_classifier_reg()
        #     loss += self.config.hp.synaptic_strength * reg

        # self.perform_optim_step(loss, 'classifier', retain_graph=True)
        # self.perform_optim_step(loss, 'embedder')
        if self.task_idx > 0:
            distill_loss, distill_acc = self.compute_cls_distill_loss()
            total_loss = curr_loss + self.config.hp.cls_distill.loss_coef * distill_loss

            self.writer.add_scalar('cls/distill_loss', distill_loss.item(), self.num_iters_done)
            self.writer.add_scalar('cls/distill_acc', distill_acc.item(), self.num_iters_done)
        else:
            total_loss = curr_loss

        self.perform_optim_step(total_loss, 'classifier')

        self.writer.add_scalar('cls/curr_oss', curr_loss.item(), self.num_iters_done)
        self.writer.add_scalar('cls/curr_acc', curr_acc.item(), self.num_iters_done)

    def creativity_step(self) -> Tensor:
        if not self.config.hp.get('creativity.enabled'): return
        if self.num_iters_done < self.config.hp.creativity.start_iter: return

        # Computes creativity loss. Creativity loss is about making Classifier unsure about samples,
        # generated from unseen attributes, but these samples should seem real to Discriminator.
        n_samples = self.config.hp.creativity.hall_batch_size

        # Sample twice as more examples
        y_from = np.random.choice(self.seen_classes, size=n_samples * 3, replace=True)
        y_to = np.random.choice(self.seen_classes, size=n_samples * 3, replace=True)

        # Let's remove overlapping classes for cleaner hallucinations
        overlaps = y_from == y_to
        y_from, y_to = y_from[~overlaps], y_to[~overlaps]

        # Removing extra samples if they exist
        y_from, y_to = y_from[:n_samples], y_to[:n_samples]
        assert len(y_from) == len(y_to)

        # Interpolating attributes
        alpha = np.random.rand(len(y_from)).astype(np.float32)
        alpha = 0.6 * alpha + 0.2 # Now it's random uniform on [0.2, 0.8]
        alpha = torch.tensor(alpha).to(self.device_name).unsqueeze(1)
        hall_attrs = self.model.attrs[y_from] * alpha + self.model.attrs[y_to] * (1 - alpha)
        hall_attrs = torch.tensor(hall_attrs).to(self.device_name)

        z = self.model.generator.sample_noise(len(hall_attrs)).to(self.device_name)
        hall_samples = self.model.generator.forward_with_attr(z, hall_attrs)
        adv_logits, cls_logits = self.model.discriminator(hall_samples)
        cls_log_probs = F.log_softmax(cls_logits, dim=1)

        adv_loss = -adv_logits.mean()
        entropy = (cls_log_probs.exp() @ cls_log_probs.t()).sum(dim=1).mean()

        loss = self.config.hp.creativity.adv_coef * adv_loss + self.config.hp.creativity.entropy_coef * entropy

        self.perform_optim_step(loss, 'generator')

        self.writer.add_scalar('creativity/adv_loss', adv_loss.item(), self.num_iters_done)
        self.writer.add_scalar('creativity/entropy', entropy.item(), self.num_iters_done)

    def compute_classifier_reg(self) -> Tensor:
        if self.config.hp.reg_strategy == 'mas':
            return self.compute_classifier_mas_reg()
        elif self.config.hp.reg_strategy == 'ewc':
            return self.compute_classifier_ewc_reg()
        else:
            raise NotImplementedError(f'Unknown regularization strategy: {self.config.hp.reg_strategy}')

    def compute_cls_distill_loss(self) -> Tuple[Tensor, Tensor]:
        with torch.no_grad():
            y = random.choices(self.learned_classes, k=self.config.hp.cls_distill.batch_size)
            y = torch.tensor(y).to(self.device_name)
            x_fake = self.model.sample(y)
            logits_prev = self.prev_model.compute_pruned_predictions(x_fake, self.seen_classes_mask)

        logits_curr = self.model.compute_pruned_predictions(x_fake, self.seen_classes_mask)
        loss = F.mse_loss(logits_curr, logits_prev)
        acc = (logits_curr.argmax(dim=1) == y).float().mean().detach().cpu()

        return loss, acc

    def compute_classifier_mas_reg(self) -> Tensor:
        weights_curr = torch.cat([p.view(-1) for p in self.model.embedder.parameters()])
        reg = torch.dot((weights_curr - self.weights_prev).pow(2), self.mse_grad)

        return reg

    def compute_classifier_ewc_reg(self) -> Tensor:
        embedder_size = get_number_of_parameters(self.model.embedder)
        keep_prob = self.config.hp.get('fisher_keep_prob', 1.)
        weights_curr = torch.cat([p.view(-1) for p in self.model.embedder.parameters()])

        body_fisher = self.fisher[:embedder_size]
        body_weights_curr = weights_curr[:embedder_size]
        body_weights_prev = self.weights_prev[:embedder_size]

        if keep_prob < 1:
            body_fisher = F.dropout(body_fisher, keep_prob)

        reg = torch.dot((body_weights_curr - body_weights_prev).pow(2), body_fisher)

        return reg

    def perform_optim_step(self, loss, module_name: str, retain_graph: bool=False):
        self.optim[module_name].zero_grad()
        loss.backward(retain_graph=retain_graph)

        if self.config.hp.grad_clipping.has(module_name):
            grad_clip_val = self.config.hp.grad_clipping.has(module_name)
            grad_norm = clip_grad_norm_(self.get_parameters(module_name), grad_clip_val)
            self.writer.add_scalar(f'grad_norms/{module_name}', grad_norm, self.num_iters_done)

        self.optim[module_name].step()
