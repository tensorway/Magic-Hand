from __future__ import print_function, absolute_import

import os
import argparse
import time
import matplotlib.pyplot as plt

import torch
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torchvision.datasets as datasets

from pose import Bar
from pose.utils.logger import Logger, savefig
from pose.utils.evaluation import accuracy, AverageMeter, final_preds
from pose.utils.misc import save_checkpoint, save_pred, adjust_learning_rate
from pose.utils.osutils import mkdir_p, isfile, isdir, join
from pose.utils.imutils import batch_with_heatmap
from pose.utils.transforms import fliplr, flip_back
import pose.models as models
import pose.datasets as datasets

model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))

idx = [1,2,3,4,5,6,11,12,15,16]

best_acc = 0

def load_teacher_network(arch, stacks, blocks, t_checkpoint):
    tmodel = models.__dict__[arch](num_stacks=stacks, num_blocks=blocks, num_classes=16, mobile=False)
    tmodel = torch.nn.DataParallel(tmodel).cuda()

    checkpoint = torch.load(t_checkpoint)
    tmodel.load_state_dict(checkpoint['state_dict'])
    tmodel.eval()
    return tmodel


def main(args):
    global best_acc

    # create checkpoint dir
    if not isdir(args.checkpoint):
        mkdir_p(args.checkpoint)

    # load teacher network
    print("==> creating teacher model '{}', stacks={}, blocks={}".format(args.arch, args.teacher_stack, args.blocks))
    tmodel = load_teacher_network(args.arch, args.teacher_stack, args.blocks, args.teacher_checkpoint)

    # create model
    print("==> creating student model '{}', stacks={}, blocks={}".format(args.arch, args.stacks, args.blocks))
    model = models.__dict__[args.arch](num_stacks=args.stacks, num_blocks=args.blocks, num_classes=args.num_classes, mobile=args.mobile)

    model = torch.nn.DataParallel(model).cuda()

    # define loss function (criterion) and optimizer
    criterion = torch.nn.MSELoss(size_average=True).cuda()

    optimizer = torch.optim.RMSprop(model.parameters(), 
                                lr=args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    title = 'mpii-' + args.arch
    if args.resume:
        if isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_acc = checkpoint['best_acc']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
            logger = Logger(join(args.checkpoint, 'log.txt'), title=title, resume=True)
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
    else:        
        logger = Logger(join(args.checkpoint, 'log.txt'), title=title)
        logger.set_names(['Epoch', 'LR', 'Train Loss', 'Val Loss', 'Train Acc', 'Val Acc'])

    cudnn.benchmark = True
    print('    Total params: %.2fM' % (sum(p.numel() for p in model.parameters())/1000000.0))

    # Data loading code
    train_loader = torch.utils.data.DataLoader(
        datasets.Mpii('data/mpii/mpii_annotations.json', 'data/mpii/images',
                      sigma=args.sigma, label_type=args.label_type,
                      unlabeled_folder=args.unlabeled_data,
                      inp_res=args.in_res, out_res=args.in_res//4),
        batch_size=args.train_batch, shuffle=True,
        num_workers=args.workers, pin_memory=True)
    
    val_loader = torch.utils.data.DataLoader(
        datasets.Mpii('data/mpii/mpii_annotations.json', 'data/mpii/images',
                      sigma=args.sigma, label_type=args.label_type, train=False,
                      inp_res=args.in_res, out_res=args.in_res // 4),
        batch_size=args.test_batch, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    if os.path.exists(args.unlabeled_data):
        print("==> loading training data from mpii {}, unlabeled data {}, val data {}".format(
                train_loader.dataset.__len__(), len(os.listdir(args.unlabeled_data)), val_loader.dataset.__len__()))
    else:
        print("==> loading training data from mpii {}, unlabeled data {}, val data {}".format(
                train_loader.dataset.__len__(), 0, val_loader.dataset.__len__()))

    if args.evaluate:
        print('\nEvaluation only') 
        loss, acc, predictions = validate(val_loader, model, criterion, args.num_classes, args.in_res//4, args.debug, args.flip)
        save_pred(predictions, checkpoint=args.checkpoint)
        return

    lr = args.lr
    for epoch in range(args.start_epoch, args.epochs):
        lr = adjust_learning_rate(optimizer, epoch, lr, args.schedule, args.gamma)
        print('\nEpoch: %d | LR: %.8f' % (epoch + 1, lr))

        # decay sigma
        if args.sigma_decay > 0:
            train_loader.dataset.sigma *=  args.sigma_decay
            val_loader.dataset.sigma *=  args.sigma_decay

        # train for one epoch
        train_loss, train_acc = train(train_loader, model, tmodel, criterion, optimizer, args.kdloss_alpha, args.debug, args.flip)

        # evaluate on validation set
        valid_loss, valid_acc, predictions = validate(val_loader, model, criterion, args.num_classes,
                                                      args.in_res//4, args.debug, args.flip)

        # append logger file
        logger.append([epoch + 1, lr, train_loss, valid_loss, train_acc, valid_acc])

        # remember best acc and save checkpoint
        is_best = valid_acc > best_acc
        best_acc = max(valid_acc, best_acc)
        save_checkpoint({
            'epoch': epoch + 1,
            'arch': args.arch,
            'state_dict': model.state_dict(),
            'best_acc': best_acc,
            'optimizer' : optimizer.state_dict(),
        }, predictions, is_best, checkpoint=args.checkpoint)

    logger.close()
    logger.plot(['Train Acc', 'Val Acc'])
    savefig(os.path.join(args.checkpoint, 'log.eps'))



def loss_with_mask(criterion, predict, target, mask):
    predict_x = predict.permute(1, 2, 3, 0)
    predict_x = predict_x * mask
    predict_x = predict_x.permute(3, 0, 1, 2)

    loss = criterion(predict_x, target)
    return loss



def train(train_loader, model, tmodel, criterion, optimizer, kdloss_alpha, debug=False, flip=True):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    kdlosses = AverageMeter()
    unkdlosses = AverageMeter()
    tslosses = AverageMeter()
    gtlosses = AverageMeter()
    acces = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()

    gt_win, pred_win = None, None
    bar = Bar('Processing', max=len(train_loader))
    for i, (inputs, target, meta) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        input_var = torch.autograd.Variable(inputs.cuda())
        target_var = torch.autograd.Variable(target.cuda(async=True))

        # compute output
        output = model(input_var)
        score_map = output[-1].data.cpu()

        # compute teacher network output
        toutput = tmodel(input_var)
        toutput = toutput[-1].detach()

        # lmse : student vs ground truth
        # gtmask will filter out the samples without ground truth
        gtloss = torch.tensor(0.0).cuda()
        kdloss = torch.tensor(0.0).cuda()
        kdloss_unlabeled = torch.tensor(0.0).cuda()
        unkdloss_alpha = 1.0
        gtmask = meta['gtmask']

        train_batch = score_map.shape[0]

        for j in range(0, len(output)):
            _output = output[j]
            for i in range(gtmask.shape[0]):
                if gtmask[i] < 0.1:
                    # unlabeled data, gtmask=0.0, kdloss only
                    # need to dividen train_batch to keep number equal
                    kdloss_unlabeled += criterion(_output[i,:,:,:], toutput[i, :,:,:])/train_batch
                else:
                    # labeled data: kdloss + gtloss
                    gtloss += criterion(_output[i,:,:,:], target_var[i, :,:,:])/train_batch
                    kdloss += criterion(_output[i,:,:,:], toutput[i,:,:,:])/train_batch

        loss_labeled = kdloss_alpha * (kdloss) + (1 - kdloss_alpha)*gtloss
        total_loss   = loss_labeled + unkdloss_alpha * kdloss_unlabeled

        acc = accuracy(score_map, target, idx)

        if debug: # visualize groundtruth and predictions
            gt_batch_img = batch_with_heatmap(inputs, target)
            pred_batch_img = batch_with_heatmap(inputs, score_map)
            teacher_batch_img = batch_with_heatmap(inputs, toutput)
            if not gt_win or not pred_win or not pred_teacher:
                ax1 = plt.subplot(131)
                ax1.title.set_text('Groundtruth')
                gt_win = plt.imshow(gt_batch_img)
                ax2 = plt.subplot(132)
                ax2.title.set_text('Prediction')
                pred_win = plt.imshow(pred_batch_img)
                ax2 = plt.subplot(133)
                ax2.title.set_text('teacher')
                pred_teacher = plt.imshow(teacher_batch_img)
            else:
                gt_win.set_data(gt_batch_img)
                pred_win.set_data(pred_batch_img)
                pred_teacher.set_data(teacher_batch_img)
            plt.pause(.05)
            plt.draw()

        # measure accuracy and record loss
        gtlosses.update(gtloss.item(), inputs.size(0))
        kdlosses.update(kdloss.item(), inputs.size(0))
        unkdlosses.update(kdloss_unlabeled.item(), inputs.size(0))
        losses.update(total_loss.item(), inputs.size(0))
        acces.update(acc[0], inputs.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        total_loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # plot progress
        bar.suffix  = '({batch}/{size}) Data: {data:.6f}s | Batch: {bt:.3f}s | Total: {total:} | ETA: {eta:} ' \
                      '| Loss: {loss:.6f} | KdLoss:{kdloss:.6f}| unKdLoss:{unkdloss:.6f}| GtLoss:{gtloss:.6f} | Acc: {acc: .4f}'.format(
                    batch=i + 1,
                    size=len(train_loader),
                    data=data_time.val,
                    bt=batch_time.val,
                    total=bar.elapsed_td,
                    eta=bar.eta_td,
                    loss=losses.avg,
                    kdloss=kdlosses.avg,
                    unkdloss=unkdlosses.avg,
                    tsloss=tslosses.avg,
                    gtloss=gtlosses.avg,
                    acc=acces.avg
                    )
        bar.next()

    bar.finish()
    return losses.avg, acces.avg


def validate(val_loader, model, criterion, num_classes, out_res, debug=False, flip=True):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    acces = AverageMeter()

    # predictions
    predictions = torch.Tensor(val_loader.dataset.__len__(), num_classes, 2)

    # switch to evaluate mode
    model.eval()

    gt_win, pred_win = None, None
    end = time.time()
    bar = Bar('Processing', max=len(val_loader))
    for i, (inputs, target, meta) in enumerate(val_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        target = target.cuda(async=True)

        input_var = torch.autograd.Variable(inputs.cuda(), volatile=True)
        target_var = torch.autograd.Variable(target, volatile=True)

        # compute output
        output = model(input_var)
        score_map = output[-1].data.cpu()
        if flip:
            flip_input_var = torch.autograd.Variable(
                    torch.from_numpy(fliplr(inputs.clone().numpy())).float().cuda(), 
                    volatile=True
                )
            flip_output_var = model(flip_input_var)
            flip_output = flip_back(flip_output_var[-1].data.cpu())
            score_map += flip_output

        loss = 0
        for o in output:
            loss += criterion(o, target_var)
        acc = accuracy(score_map, target.cpu(), idx)

        # generate predictions
        preds = final_preds(score_map, meta['center'], meta['scale'], [out_res, out_res])
        for n in range(score_map.size(0)):
            predictions[meta['index'][n], :, :] = preds[n, :, :]


        if debug:
            gt_batch_img = batch_with_heatmap(inputs, target)
            pred_batch_img = batch_with_heatmap(inputs, score_map)
            if not gt_win or not pred_win:
                plt.subplot(121)
                gt_win = plt.imshow(gt_batch_img)
                plt.subplot(122)
                pred_win = plt.imshow(pred_batch_img)
            else:
                gt_win.set_data(gt_batch_img)
                pred_win.set_data(pred_batch_img)
            plt.pause(.05)
            plt.draw()

        # measure accuracy and record loss
        losses.update(loss.item(), inputs.size(0))
        acces.update(acc[0], inputs.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # plot progress
        bar.suffix  = '({batch}/{size}) Data: {data:.6f}s | Batch: {bt:.3f}s | Total: {total:} | ETA: {eta:} | Loss: {loss:.4f} | Acc: {acc: .4f}'.format(
                    batch=i + 1,
                    size=len(val_loader),
                    data=data_time.val,
                    bt=batch_time.avg,
                    total=bar.elapsed_td,
                    eta=bar.eta_td,
                    loss=losses.avg,
                    acc=acces.avg
                    )
        bar.next()

    bar.finish()
    return losses.avg, acces.avg, predictions

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
    # Model structure
    parser.add_argument('--arch', '-a', metavar='ARCH', default='hg',
                        choices=model_names,
                        help='model architecture: ' +
                            ' | '.join(model_names) +
                            ' (default: resnet18)')
    parser.add_argument('-s', '--stacks', default=8, type=int, metavar='N',
                        help='Number of hourglasses to stack')
    parser.add_argument('--features', default=256, type=int, metavar='N',
                        help='Number of features in the hourglass')
    parser.add_argument('-b', '--blocks', default=1, type=int, metavar='N',
                        help='Number of residual modules at each location in the hourglass')
    parser.add_argument('--num-classes', default=16, type=int, metavar='N',
                        help='Number of keypoints')
    parser.add_argument('--mobile', default=False, type=bool, metavar='N',
                        help='use depthwise convolution in bottneck-block')
    # Training strategy
    parser.add_argument('-j', '--workers', default=1, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--epochs', default=90, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                        help='manual epoch number (useful on restarts)')
    parser.add_argument('--train-batch', default=6, type=int, metavar='N',
                        help='train batchsize')
    parser.add_argument('--test-batch', default=6, type=int, metavar='N',
                        help='test batchsize')
    parser.add_argument('--lr', '--learning-rate', default=2.5e-4, type=float,
                        metavar='LR', help='initial learning rate')
    parser.add_argument('--momentum', default=0, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--weight-decay', '--wd', default=0, type=float,
                        metavar='W', help='weight decay (default: 0)')
    parser.add_argument('--schedule', type=int, nargs='+', default=[60, 90],
                        help='Decrease learning rate at these epochs.')
    parser.add_argument('--gamma', type=float, default=0.1,
                        help='LR is multiplied by gamma on schedule.')
    # Data processing
    parser.add_argument('-f', '--flip', dest='flip', action='store_true',
                        help='flip the input during validation')
    parser.add_argument('--sigma', type=float, default=1,
                        help='Groundtruth Gaussian sigma.')
    parser.add_argument('--sigma-decay', type=float, default=0,
                        help='Sigma decay rate for each epoch.')
    parser.add_argument('--label-type', metavar='LABELTYPE', default='Gaussian',
                        choices=['Gaussian', 'Cauchy'],
                        help='Labelmap dist type: (default=Gaussian)')
    parser.add_argument('--in_res', default=256, type=int,
                        choices=[256, 192],
                        help='input resolution for network')
    parser.add_argument('--teacher_checkpoint', required=True, type=str,
                        help='teacher network')
    parser.add_argument('--teacher_stack', default=8, type=int,
                        help='teacher network stack')
    parser.add_argument('--kdloss_alpha', default=0.5, type=float,
                        help='weight of kdloss')
    parser.add_argument('--unlabeled_data', default='', type=str,
                        help='unlabeled data address')

    # Miscs
    parser.add_argument('-c', '--checkpoint', default='checkpoint', type=str, metavar='PATH',
                        help='path to save checkpoint (default: checkpoint)')
    parser.add_argument('--resume', default='', type=str, metavar='PATH',
                        help='path to latest checkpoint (default: none)')
    parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                        help='evaluate model on validation set')
    parser.add_argument('-d', '--debug', dest='debug', action='store_true',
                        help='show intermediate results')

    main(parser.parse_args())
