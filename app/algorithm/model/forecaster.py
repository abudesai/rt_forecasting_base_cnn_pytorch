
import numpy as np, pandas as pd
import math
import joblib
import json
import sys
import os, warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # or any {'0', '1', '2'}
warnings.filterwarnings('ignore') 


import torch
import torch.optim as optim
from torch.nn import Flatten, Conv1d, ReLU, Linear, Embedding, Module, CrossEntropyLoss, MSELoss, Tanh, Dropout
from torch.utils.data import Dataset, DataLoader




MODEL_NAME = "forecaster_base_CNN_pytorch"


model_pred_pipeline_fname = "model_pred_pipeline.save"
model_params_fname = "model_params.save"
model_wts_fname = "model_wts.save"
history_fname = "history.json"
train_data_fname = "train_data.csv"
train_data_fname_zip = "train_data.zip"


COST_THRESHOLD = float('inf')
device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
# device = 'cpu'



def get_loss(model, device, data_loader, loss_function):
    model.eval()
    loss_total = 0
    with torch.no_grad():
        for data in data_loader:
            X, y = data[0].to(device), data[1].to(device)
            output = model(X)
            loss = loss_function(y, output)
            loss_total += loss.item()
    return loss_total / len(data_loader)


def get_patience_factor(N): 
    # magic number - just picked through trial and error
    if N < 100: return 30
    patience = int(37 - math.log(N, 1.5))
    return patience


class CustomDataset(Dataset):
    def __init__(self, x, y):
        self.x = x
        self.y = y
    def __getitem__(self,index):
        # Get one item from the dataset
        return self.x[index], self.y[index]
    def __len__(self):
        return len(self.x)
    


class Forecaster(): 
    MIN_HISTORY_LEN = 60        # in epochs

    def __init__(self, 
                encode_len, 
                decode_len, 
                feat_dim, 
                latent_dim,
                activation,
                **kwargs ):
        
        self.encode_len = encode_len
        self.decode_len = decode_len
        self.feat_dim = feat_dim
        self.latent_dim = latent_dim
        self.activation = activation
        self.batch_size = 64

        self.net = Net(feat_dim = self.feat_dim, 
                       latent_dim = self.latent_dim, 
                       n_cnnlayers = 3, 
                       encode_len = self.encode_len,
                       activation = self.activation,
                       ) 
        
        self.net.to(device)
        # print(self.net.get_num_parameters()) ; sys.exit()
        self.criterion = MSELoss()
        self.optimizer = optim.Adam( self.net.parameters() )
        self.print_period = 1
        

    def fit(self, train_X, train_y, valid_X=None, valid_y=None, max_epochs=100, verbose=0):        
        
        patience = get_patience_factor(train_X.shape[0])
        # print(f"{patience=}")
        
        # print(train_X.shape, train_y.shape)
        # if valid_X is not None: print(valid_X.shape, valid_y.shape)
        
        train_X, train_y = torch.FloatTensor(train_X), torch.FloatTensor(train_y)
        train_dataset = CustomDataset(train_X, train_y)
        train_loader = DataLoader(dataset=train_dataset, batch_size=int(self.batch_size), shuffle=True)        
        
        if valid_X is not None and valid_y is not None:
            valid_X, valid_y = torch.FloatTensor(valid_X), torch.FloatTensor(valid_y)   
            valid_dataset = CustomDataset(valid_X, valid_y)
            valid_loader = DataLoader(dataset=valid_dataset, batch_size=int(self.batch_size),  shuffle=True)
        else:
            valid_loader = None

        losses = self._run_training(train_loader, valid_loader, max_epochs,
                           use_early_stopping=True, patience=patience,
                           verbose=verbose)
        return losses
    
    
    def _run_training(self, train_loader, valid_loader, max_epochs,
                      use_early_stopping=True, patience=10, verbose=1):
        
        best_loss = 1e7
        losses = []
        min_epochs = 10
        for epoch in range(max_epochs):
            self.net.train()
            for data in train_loader:
                X,  y = data[0].to(device), data[1].to(device)
                # print(inputs); sys.exit()
                # Feed Forward
                preds = self.net(X)
                # Loss Calculation
                loss = self.criterion(y, preds)
                # Clear the gradient buffer (we don't want to accumulate gradients)
                self.optimizer.zero_grad()
                # Backpropagation
                loss.backward()
                # Weight Update: w <-- w - lr * gradient
                self.optimizer.step()
                
            current_loss = loss.item()            
            
            if use_early_stopping:
                # Early stopping
                if valid_loader is not None:
                    current_loss = get_loss(self.net, device, valid_loader, self.criterion)
                losses.append({"epoch": epoch, "loss": current_loss})
                if current_loss < best_loss:
                    trigger_times = 0
                    best_loss = current_loss
                else:
                    trigger_times += 1
                    if trigger_times >= patience and epoch >= min_epochs:
                        if verbose == 1: print(f'Early stopping after {epoch=}!')
                        return losses
                
            else:
                losses.append({"epoch": epoch, "loss": current_loss})
            # Show progress
            if verbose == 1:
                if epoch % self.print_period == 0 or epoch == max_epochs-1:
                    print(f'Epoch: {epoch+1}/{max_epochs}, loss: {np.round(loss.item(), 5)}')
            
        return losses   
        
    
    def predict(self, data):       
        X, y = data['X'], data['y']  
        # X = torch.FloatTensor(X).to(device)
        # preds = self.net(X).detach().cpu().numpy()
        # preds = preds[:, -self.decode_len:]
        
        pred_X, pred_y = torch.FloatTensor(X), torch.FloatTensor(y)
        pred_dataset = CustomDataset(pred_X, pred_y)
        pred_loader = DataLoader(dataset=pred_dataset, batch_size=int(self.batch_size), shuffle=False)  
        all_preds = []  
        for data in pred_loader: 
            X,  y = data[0].to(device), data[1].to(device)
            preds = self.net(X).detach().cpu().numpy()
            preds = preds[:, -self.decode_len:]
            all_preds.append(preds)
        
        preds = np.concatenate(all_preds, axis=0)
        return preds
    

    def summary(self):
        self.model.summary()
        
    
    def evaluate(self, test_data):         
        """Evaluate the model and return the loss and metrics"""
        x_test, y_test = test_data['X'], test_data['y']  
        if self.net is not None:
            x_test, y_test = torch.FloatTensor(x_test), torch.FloatTensor(y_test)
            dataset = CustomDataset(x_test, y_test)
            data_loader = DataLoader(dataset=dataset, batch_size=32, shuffle=False)
            current_loss = get_loss(self.net, device, data_loader, self.criterion)   
            return current_loss      


    def save(self, model_path): 
        model_params = {
                "encode_len": self.encode_len, 
                "decode_len": self.decode_len, 
                "feat_dim": self.feat_dim, 
                "latent_dim": self.latent_dim,
                "activation": self.activation,
        }
        joblib.dump(model_params, os.path.join(model_path, model_params_fname))
        torch.save(self.net.state_dict(), os.path.join(model_path, model_wts_fname))


    @classmethod
    def load(cls, model_path): 
        model_params = joblib.load(os.path.join(model_path, model_params_fname))
        classifier = cls(**model_params)
        classifier.net.load_state_dict(torch.load( os.path.join(model_path, model_wts_fname)))        
        return classifier


def save_model_artifacts(train_artifacts, model_artifacts_path): 
    # save model
    save_model(train_artifacts["model"], model_artifacts_path)
    # save model-specific prediction pipeline
    save_model_pred_pipeline(train_artifacts["model_pred_pipeline"], model_artifacts_path)
    # save traiing history
    save_training_history(train_artifacts["train_history"], model_artifacts_path)
    # save training data
    save_training_data(train_artifacts["train_data"], model_artifacts_path)
    

def save_model(model, model_path):    
    model.save(model_path) 
    

def save_model_pred_pipeline(pipeline, model_path): 
    joblib.dump(pipeline, os.path.join(model_path, model_pred_pipeline_fname))
    

def load_model(model_path):     
    try: 
        model = Forecaster.load(model_path)
    except: 
        raise Exception(f'''Error loading the trained {MODEL_NAME} model. 
            Do you have the right trained model in path: {model_path}?''')
    return model


def save_training_history(history, f_path): 
    with open( os.path.join(f_path, history_fname), mode='w') as f:
        f.write( json.dumps(history, indent=2) )


def save_training_data(train_data, model_artifacts_path):
    compression_opts = { "method":'zip',  "archive_name": train_data_fname }      
    train_data.to_csv(os.path.join(model_artifacts_path, train_data_fname_zip), 
            index=False,  compression=compression_opts) 



def get_data_based_model_params(train_data): 
    ''' 
        Set any model parameters that are data dependent. 
        For example, number of layers or neurons in a neural network as a function of data shape.
    '''     
    return {
        "feat_dim": train_data['X'].shape[2], 
        "encode_len": train_data['X'].shape[1]
        }




class Net(Module):
    def __init__(self, feat_dim, latent_dim, n_cnnlayers, encode_len, activation):
        super(Net, self).__init__()
        self.feat_dim = feat_dim
        self.latent_dim = latent_dim
        self.n_rnnlayers = n_cnnlayers
        self.encode_len = encode_len
        self.activation = activation
        
        dim1 = 100
        dim2 = 50
        dim3 = 25
        
        # self.conv1 = Conv1d(self.feat_dim, 15, kernel_size=2, stride=1)
        self.conv1 = Conv1d(in_channels=self.feat_dim, out_channels=dim1, kernel_size=4, stride=1, padding='same')
        self.conv2 = Conv1d(in_channels=dim1, out_channels=dim2, kernel_size=8, stride=1, padding='same')
        self.conv3 = Conv1d(in_channels=dim2, out_channels=dim3, kernel_size=16, stride=1, padding='same')
        self.relu = ReLU()
        self.dropout = Dropout()
        self.fc = Linear(in_features=dim3*self.encode_len,  out_features=self.encode_len)
        
        # self.fc1 = Linear(in_features=dim3*self.encode_len, out_features=10)
        # self.fc2 = Linear(in_features=10, out_features=self.encode_len)
        self.flatten = Flatten()

    
    def forward(self, X):
        x = X.permute(0, 2, 1)
        
        x = self.conv1(x)
        # x = self.relu(x)
        x = self.conv2(x)
        # x = self.dropout(x)
        x = self.conv3(x)
        
        x = x.permute(0,2,1)
        x = x[:, -self.encode_len:, :]
        
        x = self.flatten(x)        
        x = self.relu(x)
        x = self.fc(x)
        # x = self.fc1(x)
        # x = self.relu(x)
        # x = self.fc2(x)
        # x = torch.squeeze(x, dim=-1)
        
        out = x
        return out
    
               
    def get_num_parameters(self):
        pp=0
        for p in list(self.parameters()):
            nn=1
            for s in list(p.size()):                
                nn = nn*s
            pp += nn
        return pp      

    def get_activation(self): 
        if self.activation == 'relu':
            return ReLU()
        elif self.activation == 'tanh':
            return Tanh()
        else: 
            raise ValueError(f"Activation {self.activation} is unrecognized. Must be either 'tanh' or 'relu'.")


if __name__ == "__main__": 
    
    
    N = 100
    T = 25
    D = 3
    
    model = Net(
        feat_dim=D,
        latent_dim=13,
        n_cnnlayers=2,
        encode_len=10, 
        activation='relu',
    )
    model.to(device=device)
    
    
    X = torch.from_numpy(np.random.randn(N, T, D).astype(np.float32)).to(device)
    
    preds = model(X)
    print(preds.shape)