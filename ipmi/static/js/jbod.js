function setupConnection(server_ip, api_key) {
    var connObj = {
        api_key: api_key,
        ip: server_ip,
        session: ''
    }
    serverStatusAPI(connObj);
};

function serverStatusAPI(connectionObj) {
    console.log("connecting to ws://" + connectionObj.ip + "/websocket");
    const hostWS = new WebSocket("ws://" + connectionObj.ip + "/websocket");
    hostWS.onopen = function() {
      // connect to server websocket
      hostWS.send(JSON.stringify({"msg": "connect", "version": "1", "support": ["1"]}));
      console.log("Connect message is sent...");
    };
    hostWS.onmessage = function (evt) {
      var recvObj = JSON.parse(evt.data);
      if (recvObj.msg == "connected") {
        connectionObj.session = recvObj.session;
        console.log("wsSessionId: " + connectionObj.session);
        wsAuthenticate(hostWS, connectionObj, function(ws) {
            // connection now established
            ws.send(JSON.stringify({
              "id": connectionObj.session,
              "msg": "method",
              "method": "system.state",
            }));
            // Get initial value and then subscribe to event stream
            ws.onmessage = function(evt) {
                var updateRecv = JSON.parse(evt.data);
                console.log(evt.data);

                // result is received on initial request
                if (updateRecv.hasOwnProperty('result')) {
                    updateStatusIcon(updateRecv.result);
                    // subscribe to event listener
                    ws.send(JSON.stringify({
                        "id": connectionObj.session,
                        "name": "system",
                        "msg": "sub"
                    }));

                // msg = 'ready' when first subscribed; new events msg = 'changed'
                } else if (updateRecv.hasOwnProperty('msg')) {
                    if (updateRecv.msg != 'ready' && updateRecv.hasOwnProperty('id')) {
                        updateStatusIcon(updateRecv.id.toUpperCase());
                    }
                }
            };
            ws.onclose = function(evt) {
                console.log("Authenticated connection was closed???");
                console.log(evt);
            };
        });
      }
    };
    hostWS.onclose = function(evt) {
      // websocket is closed.
      console.log("Connection is closed...");
      console.log(evt);
    };
};

function wsAuthenticate(ws, connectionObj, authSuccessCallback) {
  ws.send(JSON.stringify({
    "id": connectionObj.session,
    "msg": "method",
    "method": "auth.login_with_api_key",
    "params": [connectionObj.api_key]
  }));
  ws.onmessage = function (evt) {
    var authRecvObj = JSON.parse(evt.data);
    console.log("authRecvObj: ");
    console.log(authRecvObj);
    if (authRecvObj.result) {
      console.log("Authentication successful!");
      return authSuccessCallback(ws);
    } else {
      console.log("Authentication failed!");
      return;
    }
  }
}

function updateStatusIcon(statusMsg) {
    var statusIcon = document.getElementById('host-status-icon');
    switch (statusMsg) {
      case 'READY':
        statusIcon.className = 'fa fa-check-circle';
        break;
      case 'SHUTTING_DOWN':
        statusIcon.className = 'fa fa-arrow-circle-down';
        break;
      case 'BOOTING':
        statusIcon.className = 'fa fa-arrow-circle-up';
        break;
      default:
        statusIcon.className = 'fa fa-exclamation-circle';
        break;
    }
}