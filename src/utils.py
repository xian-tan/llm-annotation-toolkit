import sys
import torch
import numpy as np
import tiktoken
import random

from torch_geometric.utils import to_torch_csc_tensor
from torch_geometric.nn.conv.gcn_conv import gcn_norm

from transformers import AutoModelForCausalLM, AutoTokenizer, AutoModel
tokenizer = AutoTokenizer.from_pretrained("PATH_TO_LLM")
model = AutoModelForCausalLM.from_pretrained("PATH_TO_LLM", device_map="balanced_low_0")
emb_model = AutoModel.from_pretrained("PATH_TO_LLM", device_map="balanced_low_0")


def pooling(memory_bank, seg, pooling_type):
    seg = torch.unsqueeze(seg, dim=-1).type_as(memory_bank)
    memory_bank = memory_bank * seg
    if pooling_type == "mean":
        features = torch.sum(memory_bank, dim=1)
        features = torch.div(features, torch.sum(seg, dim=1))
    elif pooling_type == "last":
        features = memory_bank[torch.arange(memory_bank.shape[0]), torch.squeeze(
            torch.sum(seg != 0, dim=1).type(torch.int64) - 1), :]
    elif pooling_type == "max":
        features = torch.max(memory_bank + (seg - 1) * sys.maxsize, dim=1)[0]
    else:
        features = memory_bank[:, 0, :]
    return features


def query_oracle(data, prompt):
    inputs = tokenizer(prompt, return_tensors="pt").to("cuda")
    outputs = model.generate(**inputs, max_new_tokens=100, eos_token_id=2, pad_token_id=2)
    classification = tokenizer.decode(outputs[0], skip_special_tokens=True).replace(prompt, "")
    class_assign = -1
    num_classes = len(data.category_names)
    for i in range(num_classes):
        if data.category_names[i] in classification:
            class_assign = i
            break
    if class_assign == -1:
        class_assign = random.randint(0, num_classes-1)
    # ground-truth labels
    # class_assign = data.y[node]

    return class_assign


def query_oracle_for_psample(data):
    pseudo_samples = []
    # generate pseudo samples for each class
    for j, class_name in enumerate(data.category_names):
        # construct prompt
        prompt = "Suppose you are an expert at " + data.domain + ". "
        prompt += "There are now the following " + data.domain + " subcategories: " + ", ".join(data.category_names) + ". "
        prompt += "Please write a " + data.entity + " of about 200 words, so that its classification best fits the " + class_name + " category."
        # print(prompt)

        # query llm
        inputs = tokenizer(prompt, return_tensors="pt").to("cuda")
        outputs = model.generate(**inputs, max_new_tokens=300, eos_token_id=2, pad_token_id=2)
        sample = tokenizer.decode(outputs[0], skip_special_tokens=True).replace(prompt, "")
        sample = sample.replace("\n", " ")
        idx = sample.find("Abstract:")
        sample = sample[idx+9:]

        pseudo_samples.append(sample)
    
    # get the embeddings for each pseudo sample
    tokenizer.pad_token = tokenizer.eos_token
    tokens = tokenizer(pseudo_samples, padding=True,
                       truncation=True, return_tensors="pt")
    output = emb_model(**tokens)
    feat = pooling(output['last_hidden_state'],
                   tokens['attention_mask'], pooling_type="max")
    
    # compute noise distribution according to pseudo sample embeddings
    similarity_feature = torch.mm(feat, feat.t())
    norm = torch.norm(feat, 2, 1, keepdim=True).add(1e-8)
    similarity_feature = torch.div(similarity_feature, norm)
    similarity_feature = torch.div(similarity_feature, norm.t())
    similarity_feature = F.normalize(similarity_feature, p=1, dim=1)
    return similarity_feature


def get_raw_text(data, node):
    return data.raw_texts[node]


def get_embeddings_from_llm(data, node_list):
    # fetch raw texts for each node from the dataset
    raw_texts = []
    for node in node_list:
        raw_text = get_raw_text(data, node)
        raw_texts.append(raw_text)
    # generate embeddings for the node corresponding raw texts
    tokenizer.pad_token = tokenizer.eos_token
    tokens = tokenizer(raw_texts, padding=True,
                       truncation=True, return_tensors="pt")
    output = emb_model(**tokens)
    embeddings = pooling(output['last_hidden_state'],
                         tokens['attention_mask'], pooling_type="mean")
    return embeddings


def count_tokens(text, encoding='cl100k_base'):
    encoding = tiktoken.get_encoding(encoding)
    num_tokens = len(encoding.encode(text))
    return num_tokens


def calculate_ranking_diff(a_rank, b_rank):
    sum_diff = 0
    for i, a in enumerate(a_rank):
        for j, b in enumerate(b_rank):
            if a == b:
                sum_diff += abs(i-j)
    return sum_diff


def feature_propagation(data, num_hops):
    edge_weight = data.edge_attr
    if 'edge_weight' in data:
        edge_weight = data.edge_weight
    adj_t = to_torch_csc_tensor(
                edge_index=data.edge_index,
                edge_attr=edge_weight,
                size=data.size(0),
            ).t()
    adj_t, _ = gcn_norm(adj_t, add_self_loops=False)

    out = data.x.clone()
    for _ in range(num_hops):
        out = adj_t @ out
    return out
