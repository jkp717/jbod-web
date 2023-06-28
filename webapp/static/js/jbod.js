$(function () {
    // keep for testing
    // addSidebarAlert('/alerts/add', {category: 'error', content: 'test message'});

    $('#connStatusPopover').find('.navbar-icon-badge').text($('#connStatusPopover').find('.popover-content-attr.disconnected').length);
    // delay updating alert badge to give ws time to connect
    setTimeout(function() {
        // check for any disconnected children and update badge icon flag
        if ($('#connStatusPopover').find('.popover-content-attr.disconnected').length > 0) {
            $('#connStatusPopover').addClass('navbar-icon-alert');
            // update the counter in the icon badge
            $('#connStatusPopover').find('.navbar-icon-badge').text($('#connStatusPopover').find('.popover-content-attr.disconnected').length);
        } else {
            $('#connStatusPopover').removeClass('navbar-icon-alert');
        }
    }, 1000);
});

function setupConnection(server_ip, api_key) {
    const tnWebSocketConnObj = {
        api_key: api_key,
        ip: server_ip,
        session: '',
        retries: 0,
    }
    serverStatusAPI(tnWebSocketConnObj);
};

function serverStatusAPI(connectionObj) {
    const hostWS = new WebSocket("ws://" + connectionObj.ip + "/websocket");
    hostWS.onopen = function() {
      // connect to server websocket
      hostWS.send(JSON.stringify({"msg": "connect", "version": "1", "support": ["1"]}));
    };
    hostWS.onmessage = function (evt) {
      var recvObj = JSON.parse(evt.data);
      if (recvObj.msg == "connected") {
        connectionObj.session = recvObj.session;
        wsAuthenticate(hostWS, connectionObj, wsAuthenticateCallback);
      }
    };
    hostWS.onclose = function(evt) {
        updateStatusIcon({
            statusMsg: "DEAD",
            popoverDiv: $('#connStatusPopover'),
            iconDiv: document.getElementById("hostConnStatus")
        });
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
    if (authRecvObj.result) {
      console.log("Calling authSuccessCallback");
      authSuccessCallback(ws, connectionObj);
    } else {
      console.log("Authentication failed!");
      updateStatusIcon({
        statusMsg: "DEAD",
        popoverDiv: $('#connStatusPopover'),
        iconDiv: document.getElementById("hostConnStatus")
      });
    }
  }
}

function wsAuthenticateCallback(ws, connObj) {
    // connection now established
    ws.send(JSON.stringify({
      "id": connObj.session,
      "msg": "method",
      "method": "system.state",
    }));
    // Get initial value and then subscribe to event stream
    ws.onmessage = function(evt) {
        connObj.retries = 0;
        var updateRecv = JSON.parse(evt.data);
        console.log("wsAuthenticateCallback onmessage:");
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
                "id": connObj.session,
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
        // attempt to reconnect to close connection
        // only retry to connect 3 times before setting connection to 'dead'
        if (connObj.retries <= 3) {
            // reconnect to websocket
            connObj.retries++;
            serverStatusAPI(connObj);
        } else {
            connObj.retries = 0;
            updateStatusIcon({
                statusMsg: "DEAD",
                popoverDiv: $('#connStatusPopover'),
                iconDiv: document.getElementById("hostConnStatus")
            });
        }
    };
};

function openAlertSidebar() {
   $('.navbar-sidebar-content').addClass('show-sidebar');
}

function closeAlertSidebar() {
    $('.navbar-sidebar-content').removeClass('show-sidebar');
}

function deleteSidebarAlert(event, alertId, deleteAlertUrl) {
    $.ajax({
    url: deleteAlertUrl + "/" + alertId,
    type: 'DELETE',
    success: function(result) {
        $(event).parent().parent().hide("fast");  // animation hiding element
        $(event).parent().parent().remove();  // removal of actual li element
        if ($('#alertSidebar > .navbar-sidebar-content > ul').find("li").length > 0) {
            $('#alertSidebar').addClass("navbar-icon-alert");
            // add alert counter to icon badge
            $('#alertSidebar').find('.navbar-icon-badge').text($('#alertSidebar > .navbar-sidebar-content > ul').find("li").length);
        } else {
            $('#alertSidebar').removeClass("navbar-icon-alert");
        }
    }
    });
}

function deleteAllSidebarAlerts(deleteAllAlertsUrl) {
    $.ajax({
    url: deleteAllAlertsUrl,
    type: 'DELETE',
    success: function(result) {
            $('#alertSidebar > .navbar-sidebar-content > ul').find("li").each(function() {
                $(this).hide("fast");
                $(this).remove();
            });
            $('#alertSidebar').removeClass("navbar-icon-alert");
        }
    });
}

function getConsolePortOptions(selectElem, portOptionsUrl) {
    $.ajax({
    url: portOptionsUrl,
    type: 'GET',
    success: function(result) {
        result.avail_ports
        $.each(result.avail_ports, function (i, item) {
            $(selectElem).append($('<option>', {
                value: result.avail_ports[i],
                text : result.avail_ports[i]
            }));
        });
    }
    });
}

function pollConsoleStatus(pollUrl, pollInterval) {
    const iconDiv = document.getElementById("controllerConnStatus");
    $.ajax({
        url: pollUrl,
        type: 'GET',
        success: function(result) {
            // update if showing disconnected but controller is alive
            if ($(iconDiv).hasClass('disconnected') && result.alive) {
                updateStatusIcon({
                    statusMsg: "READY",
                    popoverDiv: $('#connStatusPopover'),
                    iconDiv: iconDiv
                });
            // update if NOT showing disconnected but controller is not alive
            } else if (!($(iconDiv).hasClass('disconnected')) && !(result.alive)) {
                updateStatusIcon({
                    statusMsg: "DEAD",
                    popoverDiv: $('#connStatusPopover'),
                    iconDiv: iconDiv
                });
            }
            // must be inside response callback function
            setTimeout(function() { pollConsoleStatus(pollUrl, pollInterval) }, pollInterval);
        }
    });
}

function addSidebarAlert(addURL, alertData) {
    $.ajax({
        url: addURL,
        type: 'POST',
        data: JSON.stringify(alertData),
        dataType: 'json',
        success: function(result) {
            console.log(result);
        }
    });
}

function pollSidebarAlerts(pollUrl, pollInterval) {
    const ulContainer = document.querySelector("div.navbar-sidebar-content > ul");
    $.ajax({
        url: pollUrl,
        type: 'GET',
        dataType: 'json',
        success: function(result) {
            for (let i = 0; i < result.length; i++) {
                if ( $(ulContainer).find("[data-id='" + result[i].id + "']").length == 0 ) {
                    $('#alertSidebar').addClass("navbar-icon-alert");
                    const li = document.createElement("li");
                    li.dataset.id = result[i].id;
                    const div = document.createElement("div");
                    const b = document.createElement("b")
                    b.innerHTML = result[i].category;
                    const span = document.createElement("span");
                    span.className = "close-alert";
                    span.setAttribute("onclick", "deleteSidebarAlert(this," + result[i].id + ", '/alerts/delete');");
                    span.innerHTML = "&times;";
                    div.appendChild(b);
                    div.appendChild(span);
                    li.appendChild(div);
                    li.innerHTML += result[i].content + " " + result[i].id;
                    ulContainer.appendChild(li);

                    // increment counter
                    $('#alertSidebar > a > .navbar-icon-badge').text($(ulContainer).find('li').length);
                }
            }
            // must be inside response callback function
            setTimeout(function() { pollSidebarAlerts(pollUrl, pollInterval) }, pollInterval);
        }
    });
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

function updateStatusIcon(options) {
    const statusMsg = options.statusMsg;
    const iconDiv = options.iconDiv;
    const popoverDiv = options.popoverDiv;

    switch (statusMsg) {
      case 'READY':
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
        iconDiv.querySelector('i').className = 'fa fa-minus-circle';
        $(iconDiv).addClass('disconnected');
        break;
    }
    // check for any disconnected children and update badge icon flag
    if (popoverDiv.find('.popover-content-attr.disconnected').length > 0) {
        popoverDiv.addClass('navbar-icon-alert');
        // update the counter in the icon badge
        popoverDiv.find('.navbar-icon-badge').text(popoverDiv.find('.popover-content-attr.disconnected').length);
    } else {
        popoverDiv.removeClass('navbar-icon-alert');
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
    // Get the form element
    const form = document.getElementById(formElementId);
    let formObj = {};

    // Add 'submit' event handler
    form.addEventListener("submit", (event) => {
    event.preventDefault();
    // add all form inputs and their values to a json object to be sent
    for (let i = 0; i < form.elements.length; i++) {
        if (form.elements[i].tagName !== 'BUTTON') {
            formObj[form.elements[i].id] = form.elements[i].value;
        }
    }
    form.reset();
    sendJSONFormData();
    })

    function sendJSONFormData() {
        const modalXHR = new XMLHttpRequest();

        // Define what happens on successful data submission
        modalXHR.addEventListener("load", (event) => {
          // replace Form in id name with Model, ie hostStatusForm to hostStatusModal
          var modalIdSelector = "#" + formElementId.replace('Form','Modal');
          $(modalIdSelector).modal('hide');
          let resp = JSON.parse(modalXHR.responseText);
          if (resp['result'] == 'success') {
            document.querySelector('a[data-target="' + modalIdSelector + '"] > span').className = "fa fa-solid fa-check-circle";
            createAlertElements("success", resp["msg"]);
            if (formElementId == "hostSetupForm") {
              updateStatusIcon({
                statusMsg: "READY",
                popoverDiv: $('#connStatusPopover'),
                iconDiv: document.getElementById("hostConnStatus")
              });
            } else {
              updateStatusIcon({
                statusMsg: "READY",
                popoverDiv: $('#connStatusPopover'),
                iconDiv: document.getElementById("controllerConnStatus")
              });
            }
          } else {
            document.querySelector('a[data-target="' + modalIdSelector + '"] > span').className = "fa fa-solid fa-question-circle";
            createAlertElements("danger", resp["msg"]);
            if (formElementId == "hostSetupForm") {
              updateStatusIcon({
                statusMsg: resp["result"].toUpperCase(),
                popoverDiv: $('#connStatusPopover'),
                iconDiv: document.getElementById("hostConnStatus")
              });
            } else {
              updateStatusIcon({
                statusMsg: resp["result"].toUpperCase(),
                popoverDiv: $('#connStatusPopover'),
                iconDiv: document.getElementById("controllerConnStatus")
              });
            }
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
        modalXHR.send(JSON.stringify(formObj));
    }
}

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
//            console.log("width of parent " + progressBar.parent().width());
//            console.log("starting width of " + Math.round(progressBar.parent().width() * 0.3) + "px");
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
        if (calStatCode == 2 && cntAttempts <= 15) {
            ++cntAttempts;// still running
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