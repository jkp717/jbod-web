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
                    updateStatusIcon({
                        statusMsg: updateRecv.result,
                        popoverDiv: $('#connStatusPopover'),
                        iconDiv: document.getElementById("hostConnStatus")
                    });
                    // subscribe to event listener
                    ws.send(JSON.stringify({
                        "id": connectionObj.session,
                        "name": "system",
                        "msg": "sub"
                    }));

                // msg = 'ready' when first subscribed; new events msg = 'changed'
                } else if (updateRecv.hasOwnProperty('msg')) {
                    if (updateRecv.msg != 'ready' && updateRecv.hasOwnProperty('id')) {
                        updateStatusIcon({
                            statusMsg: updateRecv.id.toUpperCase(),
                            popoverDiv: $('#connStatusPopover'),
                            iconDiv: document.getElementById("hostConnStatus")
                        });
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

function updateStatusIcon(options) {
    const statusMsg = options.statusMsg;
    const iconDiv = options.iconDiv;
    const popoverDiv = options.popoverDiv;

    switch (statusMsg) {
      case 'READY':
        console.log("connection is ready...");
        iconDiv.querySelector('i').className = 'fa fa-solid fa-check-circle';
        $(iconDiv).removeClass('disconnected');
        break;
      case 'SHUTTING_DOWN':
        iconDiv.querySelector('i').className = 'fa fa-solid fa-arrow-circle-down';
        popoverDiv.data('flag', true);
        $(iconDiv).addClass('disconnected');
        break;
      case 'BOOTING':
        iconDiv.querySelector('i').className = 'fa fa-solid fa-arrow-circle-up';
        $(iconDiv).addClass('disconnected');
        break;
      case 'ERROR':
        iconDiv.querySelector('i').className = 'fa fa-solid fa-question-circle';
        $(iconDiv).addClass('disconnected');
        break;
      default:
        iconDiv.querySelector('i').className = 'fa fa-solid fa-link-slash';
        $(iconDiv).addClass('disconnected');
        break;
    }
    if (popoverDiv.find(".popover-content-attr").hasClass('disconnected')) {
        popoverDiv.addClass("navbar-popover-alert");
    }


    iconDiv.querySelector('i').title = statusMsg;
    // Destroy and then recreate popover with new content
    popoverDiv.find('a').popover('dispose');
    popoverDiv.find('a').popover({
        content: popoverDiv.find('.popover-container').html(),
        html: true
    });
}

/*
 * Welcome Page / Initial Setup
*/
function modalFormHandler(formElementId, formUrl) {
  function sendData(data) {
    const modalXHR = new XMLHttpRequest();
    // Define what happens on successful data submission
    modalXHR.addEventListener("load", (event) => {
      form.reset();
      var modalIdSelector = "#" + formElementId.replace('-form','-modal');
      $(modalIdSelector).modal('hide');
      let resp = JSON.parse(modalXHR.responseText);
      if (resp['result'] == 'success') {
        document.querySelector('a[data-target="' + modalIdSelector + '"] > span').className = "fa fa-solid fa-check-circle";
        createAlertElements("success", resp["msg"]);
        updateStatusIcon({
            statusMsg: "READY",
            popoverDiv: $('#connStatusPopover'),
            iconDiv: document.getElementById("controllerConnStatus")
        });
      } else {
        document.querySelector('a[data-target="' + modalIdSelector + '"] > span').className = "fa fa-solid fa-question-circle";
        createAlertElements("danger", resp["msg"]);
        updateStatusIcon({
            statusMsg: resp['result'].toUpperCase(),
            popoverDiv: $('#connStatusPopover'),
            iconDiv: document.getElementById("controllerConnStatus")
        });
      }
    });
    // Define what happens in case of error
    modalXHR.addEventListener("error", (event) => {
      alert('Oops! Something went wrong.');
    });
    // Set up our request
    modalXHR.open("POST", formUrl);
    // The data sent is what the user provided in the form
    modalXHR.setRequestHeader("Content-Type", 'application/json');
    modalXHR.send(JSON.stringify(data));
  }
  // Get the form element
  const form = document.getElementById(formElementId);
  // Add 'submit' event handler
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    // add all form inputs and their values to a json object to be sent
    var formObj = {};
    for (let i = 0; i < form.elements.length; i++) {
        if (form.elements[i].tagName !== 'BUTTON') {
            formObj[form.elements[i].id] = form.elements[i].value;
        }
    }
    sendData(formObj);
  });
};

function fanCalibrationHandler(requestURL, fanId) {
    // Confirmation window
    if (confirm("Start fan calibration?")) {
        const progressBar = $('.calibration-progress-modal[data-fan="' + fanId + '"]').find( ".progress-bar" );
        const XHR = new XMLHttpRequest();
        // Define what happens on successful data submission
        XHR.addEventListener("load", (event) => {
          if (XHR.status == 200) {
            let jobId = JSON.parse(XHR.responseText)['job']['id'];
            $('.calibration-progress-modal[data-fan="' + fanId + '"]').modal('show');
            console.log("width of parent " + progressBar.parent().width());
            console.log("starting width of " + Math.round(progressBar.parent().width() * 0.3) + "px");
            progressBar.animate(
                {
                  width: Math.round(progressBar.parent().width() * 0.3) + "px"
                },{
                  queue: false,
                  duration: 3000,
                  easing: 'linear',
                  complete: function() {
                    checkCalibrationStatus(fanId);
                  }
                }
            );
          }
        });
        // Define what happens in case of error
        XHR.addEventListener("error", (event) => {
          console.log("error processing request");
        });
        // Set up our request
        XHR.open("GET", requestURL);
        XHR.send();
    } else {
        console.log("Calibration job cancelled.");
    }
}

var cntAttempts = 0;

function checkCalibrationStatus(fanId) {
    const XHR = new XMLHttpRequest();
    // Define what happens on successful data submission
    XHR.addEventListener("load", (event) => {
      if (XHR.status == 200) {
        const calStatCode = JSON.parse(XHR.responseText)['status'];
        const calStatMsg = JSON.parse(XHR.responseText)['message'];
        const progressBar = $('.calibration-progress-modal[data-fan="' + fanId + '"]').find( ".progress-bar" );
        if (calStatCode == 2 && cntAttempts <= 7) {
            console.log("trying again...");
            ++cntAttempts;// still running
            console.log("current width: " +  progressBar.width());
            console.log("widening bar by " + "+=" + Math.round(progressBar.parent().width() * 0.1) + "px");
            progressBar.animate(
                {
                  width: "+=" + Math.round(progressBar.parent().width() * 0.1) + "px"
                },{
                  queue: false,
                  duration: 1000,
                  easing: 'linear',
                  complete: function() {
                    checkCalibrationStatus(fanId);
                  }
                }
            );
        } else {
            console.log("final width: " + "+=" + Math.round(progressBar.parent().width() - progressBar.width()) + "px");
            progressBar.animate(
                {
                  width: "+=" + Math.round(progressBar.parent().width() - progressBar.width()) + "px"
                },{
                  queue: false,
                  duration: 500,
                  easing: 'linear',
                  complete: function() {
                    createNotificationBanner(calStatCode, calStatMsg);
                    $('.calibration-progress-modal[data-fan="' + fanId + '"]').modal('hide');
                    cntAttempts = 0;
                  }
                }
            );
        }
      }
    });
    // Define what happens in case of error
    XHR.addEventListener("error", (event) => {
      console.log("error processing request");
    });
    // Set up our request
    XHR.open("GET", '/fan/calibrate/status?id=' + fanId);
    XHR.send();
}


function createNotificationBanner(calStatCode, message) {
    switch (parseInt(calStatCode)) {
      case 0:  // Fail
        createAlertElements('danger', message);
        break;
      case 1:  // Complete
        createAlertElements('success', message);
        break;
      case 2:  // Running
        createAlertElements('info', message);
        break;
      default:  // Unknown
        createAlertElements('warning', message);
        break;
    }
}

function createAlertElements(alertType, message) {
    const alertContainer = document.getElementById("alert-container");

    const div = document.createElement("div");
    div.className = "alert alert-" + alertType + " alert-dismissible fade show";
    div.setAttribute("role", "alert");

    const msgSpan = document.createElement("span");
    msgSpan.innerHTML = message;

    const btn = document.createElement("button");
    btn.className = "close";
    btn.setAttribute("type", "button");
    btn.setAttribute("data-dismiss", "alert");

    const span = document.createElement("span");
    span.innerHTML = '&times;'

    div.appendChild(msgSpan);
    btn.appendChild(span);
    div.appendChild(btn);
    alertContainer.appendChild(div);
}