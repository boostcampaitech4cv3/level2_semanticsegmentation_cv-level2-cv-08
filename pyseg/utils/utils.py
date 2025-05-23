# # https://github.com/wkentaro/pytorch-fcn/blob/master/torchfcn/utils.py

import numpy as np
import pandas as pd
import cv2

def _fast_hist(label_true, label_pred, n_class):
    mask = (label_true >= 0) & (label_true < n_class)
    hist = np.bincount(n_class * label_true[mask].astype(int) + label_pred[mask],
                        minlength=n_class ** 2).reshape(n_class, n_class)
    return hist


def label_accuracy_score(hist):
    """
    Returns accuracy score evaluation result.
      - [acc]: overall accuracy
      - [acc_cls]: mean accuracy
      - [mean_iu]: mean IU
      - [fwavacc]: fwavacc
    """
    acc = np.diag(hist).sum() / hist.sum()
    with np.errstate(divide='ignore', invalid='ignore'):
        acc_cls = np.diag(hist) / hist.sum(axis=1)
    acc_cls = np.nanmean(acc_cls)

    with np.errstate(divide='ignore', invalid='ignore'):
        iu = np.diag(hist) / (hist.sum(axis=1) + hist.sum(axis=0) - np.diag(hist))
    mean_iu = np.nanmean(iu)

    freq = hist.sum(axis=1) / hist.sum()
    fwavacc = (freq[freq > 0] * iu[freq > 0]).sum()
    return acc, acc_cls, mean_iu, fwavacc, iu


def add_hist(hist, label_trues, label_preds, n_class):
    """
        stack hist(confusion matrix)
    """

    for lt, lp in zip(label_trues, label_preds):
        hist += _fast_hist(lt.flatten(), lp.flatten(), n_class)

    return hist




def _fast_hist(label_true, label_pred, n_class):
    mask = (label_true >= 0) & (label_true < n_class)
    hist = np.bincount(
        n_class * label_true[mask].astype(int) +
        label_pred[mask], minlength=n_class ** 2).reshape(n_class, n_class)
    return hist


def val_viz(data_loader, exp):
    batch = next(iter(data_loader))
    for i, (image, mask, info) in enumerate(zip(*batch)):
        # Preprocess
        # - Tensor Transform -> Numpy
        image = image
        image = image.permute(1, 2, 0).numpy()
        mask = mask.numpy()
        image*=255
        
        image = image.astype(np.uint8)

        mask = np.expand_dims(mask, axis=2)
        mask = mask.astype(np.uint8)

        color_map = np.array([
            [0, 0, 0],
            [255, 0, 0],
            [0, 255, 0],
            [0, 0, 255],
            [255, 255, 0],
            [0, 255, 255],
            [255, 0, 255],
            [192, 128, 64],
            [192, 192, 128],
            [64, 64, 128],
            [128, 0, 192],
        ])

        # Main 기능
        # - segment의 bit 이미지 제작
        bit_mask = mask.copy()
        bit_mask[bit_mask>0] = 255
        bit_mask = cv2.cvtColor(bit_mask, cv2.COLOR_GRAY2RGB)

        print(image.shape, mask.shape)
        print(bit_mask.shape)

        # - 마스크, 세그먼트 원본, 배경 원본 작성
        mask = np.array(list(map(lambda x: color_map[x], mask)), dtype=np.uint8).squeeze()
        segment = cv2.bitwise_and(image, bit_mask)
        bg = cv2.subtract(image, bit_mask)

        masked_segment = cv2.addWeighted(segment, 0.5, mask, 0.5, 0)
        viz = cv2.bitwise_or(masked_segment, bg)
        
        cv2.imwrite(f"exp/{exp}/tensorboard/viz{i}.jpg", viz)



def get_result(output, file_names, preds):
    submission = pd.read_csv('./submission/sample_submission.csv', index_col=None)

    # PredictionString 대입
    for file_name, string in zip(file_names, preds):
        submission = submission.append({"image_id" : file_name, "PredictionString" : ' '.join(str(e) for e in string.tolist())}, 
                                    ignore_index=True)

    # submission.csv로 저장
    submission.to_csv(f"./submission/{output}", index=False)



# def label_accuracy_score(label_trues, label_preds, n_class):
#     """Returns accuracy score evaluation result.
#       - overall accuracy
#       - mean accuracy
#       - mean IU
#       - fwavacc
#     """
#     hist = np.zeros((n_class, n_class))
#     for lt, lp in zip(label_trues, label_preds):
#         hist += _fast_hist(lt.flatten(), lp.flatten(), n_class)
#     acc = np.diag(hist).sum() / hist.sum()
#     with np.errstate(divide='ignore', invalid='ignore'):
#         acc_cls = np.diag(hist) / hist.sum(axis=1)
#     acc_cls = np.nanmean(acc_cls)
#     with np.errstate(divide='ignore', invalid='ignore'):
#         iu = np.diag(hist) / (
#             hist.sum(axis=1) + hist.sum(axis=0) - np.diag(hist)
#         )
#     mean_iu = np.nanmean(iu)
#     freq = hist.sum(axis=1) / hist.sum()
#     fwavacc = (freq[freq > 0] * iu[freq > 0]).sum()
#     return acc, acc_cls, mean_iu, fwavacc, iu